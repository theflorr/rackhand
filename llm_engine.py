"""Real LLM Agent Engine for Rackhand

Provides live streaming inference, tool calling for real network hardware execution,
rolling context memory management, multi-provider routing (OpenAI, Anthropic, Gemini, OpenRouter),
and error recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator, Callable, Coroutine, Optional

import httpx

from models import (
    AgentDone, AgentError, AgentEvent, AgentProposal, AgentText, AgentToolCall,
    AgentToolResult, ApiScheme, ApprovalDecision, AuthStyle, DeviceSession, ProposedChange,
    ProviderConfig, Risk, Settings, ensure_agy_in_path, find_agy_executable,
)

logger = logging.getLogger("rackhand.llm_engine")


REDACT_PATTERNS = [
    (re.compile(r'\b(password(?:\s+[0-9]+)?\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(secret(?:\s+[0-9]+)?\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(snmp-server\s+community\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(pre-shared-key(?:\s+(?:hex|ascii|[0-9]+))?\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(preshared-key\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(key-string\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(md5\s+key-string\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'\b(wpa-psk(?:\s+ascii)?\s+)\S+', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'-----BEGIN[ A-Z_-]*PRIVATE KEY-----.*?-----END[ A-Z_-]*PRIVATE KEY-----', re.DOTALL), r'[REDACTED PRIVATE KEY]'),
]


def redact_sensitive_text(text: str) -> str:
    for pat, repl in REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ==============================================================================
# TOOL DEFINITIONS FOR NETWORK ENGINEERING
# ==============================================================================

NETWORK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a safe diagnostic or read-only CLI command on the connected network device "
                "(e.g. 'show version', 'show interfaces status', 'show ip interface brief', 'show vlan brief', "
                "'show mac address-table', 'show running-config', 'get system status', 'show conn')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact CLI command string to execute on the device."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_change",
            "description": (
                "Propose a configuration change that modifies the device state. "
                "This pauses execution and requests explicit human review and approval before applying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short summary of the proposed change (e.g. 'Configure Access VLAN 20 on Fa0/5')."
                    },
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Sequential list of configuration commands to enter."
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Explanation of why this change is necessary and its intended effect."
                    },
                    "risk": {
                        "type": "string",
                        "enum": ["config", "danger"],
                        "description": "Risk assessment level of this change: 'config' for standard operational changes, 'danger' for service-impacting changes (e.g. reboots, trunk pruning, ACL changes)."
                    }
                },
                "required": ["title", "commands", "rationale", "risk"]
            }
        }
    }
]

PLAN_NETWORK_TOOLS = [NETWORK_TOOLS[0]]


def is_mutating_cli_command(cmd: str) -> bool:
    """Return True if command attempts to alter device configuration or reload hardware."""
    c = cmd.strip().lower()
    mutating_prefixes = (
        "conf", "config", "wr", "write", "copy", "reload", "reboot",
        "delete", "erase", "format", "no ", "default ", "set ", "unset ",
        "install", "rollback", "archive",
        "execute reboot", "execute factoryreset", "execute restore",
    )
    return any(c.startswith(p) for p in mutating_prefixes)


# ==============================================================================
# CONTEXT MEMORY MANAGER
# ==============================================================================

class ContextMemoryManager:
    """Manages multi-turn conversation memory, output pruning, and context limits."""

    def __init__(self, max_context_tokens: int = 128000, max_turns: int = 20) -> None:
        self.max_context_tokens = max_context_tokens
        self.max_turns = max_turns
        self.sessions: dict[str, list[dict[str, Any]]] = {}

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.sessions.setdefault(session_id, [])

    def clear(self, session_id: str) -> None:
        """Clear conversation memory for a session."""
        self.sessions.pop(session_id, None)

    def set_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Set messages directly for a session (used when restoring sessions from disk)."""
        import copy
        self.sessions[session_id] = copy.deepcopy(messages)

    def clone_session(self, source_session_id: str, target_session_id: str) -> None:
        """Clone all messages from source session to target session."""
        import copy
        source_msgs = self.sessions.get(source_session_id, [])
        self.sessions[target_session_id] = copy.deepcopy(source_msgs)

    def add_message(self, session_id: str, role: str, content: Any, **kwargs: Any) -> None:
        msgs = self.get_messages(session_id)
        msg: dict[str, Any] = {"role": role, "content": content}
        for k, v in kwargs.items():
            if v is not None:
                msg[k] = v
        msgs.append(msg)
        self.prune_if_needed(session_id)

    def prune_if_needed(self, session_id: str) -> None:
        msgs = self.sessions.get(session_id, [])
        if len(msgs) <= self.max_turns:
            return

        # Always preserve index 0 (System Message)
        system_msg = msgs[0] if msgs and msgs[0].get("role") == "system" else None
        remaining = msgs[1:] if system_msg else msgs

        # Keep the most recent max_turns messages
        pruned = remaining[-self.max_turns:]

        # Ensure we never orphan tool result messages:
        # A 'tool' message must be preceded by an assistant message with tool_calls.
        while pruned and pruned[0].get("role") == "tool":
            pruned.pop(0)

        self.sessions[session_id] = ([system_msg] + pruned) if system_msg else pruned

    @staticmethod
    def truncate_tool_output(output: str, max_lines: int = 250) -> str:
        """Truncate massive command outputs (e.g. show tech) to preserve context."""
        lines = output.splitlines()
        if len(lines) <= max_lines:
            return output
        kept = lines[:max_lines]
        omitted = len(lines) - max_lines
        return "\n".join(kept) + f"\n\n... [{omitted} lines truncated for context management]"


# ==============================================================================
# REAL LLM ENGINE
# ==============================================================================

CLAUDE_STYLE_GUIDELINES = """

### Communication Style & Response Workflow (Claude Guidelines)
- Direct & Outcome-First: Lead immediately with the primary answer, finding, or action taken. Do not use conversational filler, pleasantries, or preambles (e.g. avoid "Certainly!", "Sure thing", "Let me check that for you").
- Concise & Factual: Answer routine questions and report status in 1-3 sentences of clean prose. Avoid unnecessary wrap-ups (e.g. avoid "I hope this helps! Let me know if you need anything else").
- Minimalist Formatting: Use the minimum formatting needed for clarity. Avoid over-formatting with excessive bold text, nested bullet lists, or redundant section headers. Use markdown code blocks with language tags for CLI commands and outputs.
- Investigation Workflow:
  1. Inspect First: Query the device with safe read-only commands (`run_command`) before making claims or guessing current state.
  2. Grounded Findings: Base conclusions strictly on real command output. State observations clearly.
  3. Safe Changes: Propose configuration changes via `propose_change` with exact syntax and rollback steps.
- Intellectual Honesty: If command output is unexpected or an error occurs, report it plainly without making excuses or hallucinating.
"""


def build_llm_request(
    provider: ProviderConfig,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    settings: Optional[Settings] = None,
    max_tokens: Optional[int] = None,
) -> tuple[str, dict[str, str], dict[str, Any], bool]:
    """Format request URL, headers, and payload according to provider API scheme.
    
    Returns (endpoint, headers, payload, is_anthropic).
    """
    is_anthropic = (
        getattr(provider, "scheme", None) == ApiScheme.ANTHROPIC
        or "anthropic.com" in (provider.base_url or "").lower()
    )

    base_url = (provider.base_url or ("https://api.anthropic.com" if is_anthropic else "https://api.openai.com/v1")).rstrip("/")
    api_key = provider.api_key.strip() if isinstance(provider.api_key, str) else ""

    if is_anthropic:
        if not base_url.endswith("/messages") and not base_url.endswith("/v1"):
            endpoint = f"{base_url}/v1/messages"
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/messages"
        else:
            endpoint = base_url

        auth_style = getattr(provider, "auth", None)
        is_oauth = (
            api_key.startswith("sk-ant-oat")
            or api_key.startswith("ey")
            or (auth_style == AuthStyle.BEARER and not api_key.startswith("sk-ant-"))
        )

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if is_oauth:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
            headers["user-agent"] = "claude-code/0.2.29"
        else:
            headers["x-api-key"] = api_key
        toks = max_tokens if max_tokens is not None else (provider.max_output if provider.max_output and provider.max_output > 0 else 1024)
        model = provider.model or "claude-3-5-sonnet-20241022"

        # Separate system messages for Anthropic
        system_texts = [m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]
        user_msgs = [m for m in messages if m.get("role") != "system"]

        payload: dict[str, Any] = {
            "model": model,
            "messages": user_msgs if user_msgs else [{"role": "user", "content": "hi"}],
            "max_tokens": toks,
        }
        if system_texts:
            payload["system"] = "\n\n".join(system_texts)
        if tools:
            payload["tools"] = tools
        return endpoint, headers, payload, True

    # Standard OpenAI / OpenRouter / compatible schemes
    if "chat/completions" not in base_url and not base_url.endswith("/v1"):
        endpoint = f"{base_url}/chat/completions"
    elif base_url.endswith("/v1"):
        endpoint = f"{base_url}/chat/completions"
    else:
        endpoint = base_url

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if "openrouter" in base_url.lower():
        headers["HTTP-Referer"] = "https://github.com/rackhand"
        headers["X-Title"] = "Rackhand Network Assistant"

    # Select model
    model = provider.model or "meta-llama/llama-3.3-70b-instruct"
    # Bounded max output tokens to prevent OpenRouter 402 quota rejections
    toks = max_tokens if max_tokens is not None else (provider.max_output if provider.max_output and provider.max_output > 0 else 2048)

    use_tools = tools if tools is not None else (NETWORK_TOOLS if getattr(provider, "supports_tools", False) else None)

    payload = {
        "model": model,
        "messages": messages,
        "tools": use_tools,
        "tool_choice": "auto" if use_tools else None,
        "max_tokens": toks,
        "temperature": getattr(provider, "temperature", 0.2),
    }

    if settings and getattr(settings, "reasoning_effort", None):
        effort = settings.reasoning_effort
        if effort not in ("off", "none"):
            payload["reasoning_effort"] = "high" if effort == "max" else effort
            if "openrouter" in base_url.lower():
                payload["reasoning"] = {"effort": effort}

    # Filter None
    payload = {k: v for k, v in payload.items() if v is not None}

    return endpoint, headers, payload, False


class RealLLMEngine:
    """Production LLM agent driver executing real hardware commands via live model inference."""

    def __init__(self) -> None:
        self.memory = ContextMemoryManager()
        self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._agy_conversations: dict[str, str] = {}
        self._mid_turn_prompts: dict[str, str] = {}

    def inject_mid_turn_prompt(self, session_id: str, prompt: str) -> bool:
        self._mid_turn_prompts[session_id] = prompt
        return True

    async def aclose(self) -> None:
        await self._http_client.aclose()

    def build_system_message(self, session: Optional[DeviceSession], agent_spec: Any, is_plan_mode: bool = False) -> str:
        base_prompt = getattr(agent_spec, "system_prompt", "")
        badge = getattr(agent_spec, "badge", "[AGENT]")
        name = getattr(agent_spec, "name", "Specialist Agent")
        agent_id = str(getattr(agent_spec, "id", "")).lower()

        # Inject real hardware context
        if session:
            hw_context = (
                f"\n\n### LIVE CONNECTED HARDWARE STATUS\n"
                f"- **Target Hostname**: {session.hostname}\n"
                f"- **Hardware Vendor**: {session.profile.vendor.value.upper()}\n"
                f"- **Transport**: {session.profile.transport.value.upper()}\n"
                f"- **Current CLI Prompt**: `{session.prompt}`\n"
                f"- **Execution Mode**: {'Unprivileged User EXEC (>)' if session.prompt.endswith('>') else 'Privileged EXEC / Config (#)'}\n"
                f"\n"
                f"You have direct access to the `run_command` tool to execute real CLI commands on `{session.hostname}`.\n"
                f"Query the device using `run_command` when diagnostic data is needed before drawing conclusions.\n"
                f"If configuration changes are requested, use the `propose_change` tool so the user can review and approve them."
            )
        else:
            hw_context = (
                "\n\n### HARDWARE STATUS\n"
                "No live device is currently connected. Answer technical network questions, "
                "troubleshooting steps, command syntax, configuration design, and architecture directly."
            )

        plan_notice = ""
        if is_plan_mode or "plan" in agent_id:
            plan_notice = (
                "\n\n### CURRENT MODE: PLAN MODE (STRICT READ-ONLY)\n"
                "- Plan Mode is active. Operation is strictly read-only analysis, discovery, and planning.\n"
                "- Use `run_command` ONLY for safe diagnostic commands ('show', 'get', 'display').\n"
                "- Configuration changes and `propose_change` are disabled.\n"
                "- Output a step-by-step implementation plan, ask discovery questions, and prompt the user to use /build when done."
            )

        return f"{base_prompt}{hw_context}{plan_notice}{CLAUDE_STYLE_GUIDELINES}"

    async def run_turn(
        self,
        prompt: str,
        session: DeviceSession,
        provider: ProviderConfig,
        settings: Settings,
        agent_spec: Any,
        run_command_fn: Callable[[str], Coroutine[Any, Any, Any]],
        propose_change_fn: Callable[[ProposedChange], Coroutine[Any, Any, ApprovalDecision]],
        session_id: Optional[str] = None,
        is_plan_mode: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a full agent turn with real LLM streaming and hardware tool calling."""
        sess_id = session_id or session.id
        badge = getattr(agent_spec, "badge", "[AGENT]")
        name = getattr(agent_spec, "name", "Specialist Agent")

        # 1. Initialize Context & System Prompt
        history = self.memory.get_messages(sess_id)
        if not history or history[0].get("role") != "system":
            system_content = self.build_system_message(session, agent_spec, is_plan_mode=is_plan_mode)
            if history:
                history.insert(0, {"role": "system", "content": system_content})
            else:
                self.memory.add_message(sess_id, "system", system_content)
        else:
            # Update live prompt in case device mode transitioned
            history[0]["content"] = self.build_system_message(session, agent_spec, is_plan_mode=is_plan_mode)

        # Append current user prompt
        self.memory.add_message(sess_id, "user", prompt)

        # 2. Check for Google Antigravity provider (AGY CLI bridge)
        if provider.scheme == ApiScheme.ANTIGRAVITY or provider.spec_id == "antigravity" or provider.base_url == "local:agy" or "antigravity" in provider.name.lower():
            async for ev in self._run_antigravity_turn(
                prompt=prompt,
                session=session,
                provider=provider,
                settings=settings,
                agent_spec=agent_spec,
                run_command_fn=run_command_fn,
                propose_change_fn=propose_change_fn,
                session_id=sess_id,
                is_plan_mode=is_plan_mode,
            ):
                yield ev
            return

        # 3. Check Provider Credentials & Endpoint
        api_key = provider.api_key.strip() if isinstance(provider.api_key, str) else ""
        if not api_key:
            yield AgentError(
                f"{badge} {name}: No API key or access token is configured for provider {provider.name}. "
                f"Press F2 to select a provider with valid credentials (such as OpenRouter or Claude), or scan your local system."
            )
            yield AgentDone(0, 0)
            return

        total_prompt_tokens = 0
        total_completion_tokens = 0
        max_steps = getattr(settings, "max_agent_steps", 10) or 10

        # 3. Agent Loop (LLM call -> Tool Execution -> Repeat until done)
        for step in range(max_steps):
            # Check for mid-turn injected prompt (Alt+M after current tool call or step)
            injected = self._mid_turn_prompts.pop(sess_id, None)
            if injected:
                self.memory.add_message(sess_id, "user", injected)

            current_messages = list(self.memory.get_messages(sess_id))

            # Build Request Payload based on provider scheme
            tools_to_use = PLAN_NETWORK_TOOLS if is_plan_mode else (NETWORK_TOOLS if provider.supports_tools else None)
            endpoint, headers, payload = self._build_request(provider, current_messages, tools=tools_to_use, settings=settings)

            response_data = None
            try:
                resp = await self._http_client.post(endpoint, headers=headers, json=payload)
                if resp.status_code != 200:
                    err_text = self._format_api_error(resp.status_code, resp.text, provider)
                    yield AgentError(f"{badge} {err_text}")
                    yield AgentDone(total_prompt_tokens, total_completion_tokens)
                    return
                response_data = resp.json()
            except httpx.TimeoutException:
                yield AgentError(f"{badge} Request to {provider.name} timed out after 30 seconds.")
                yield AgentDone(total_prompt_tokens, total_completion_tokens)
                return
            except Exception as exc:
                yield AgentError(f"{badge} Connection Error: {exc}")
                yield AgentDone(total_prompt_tokens, total_completion_tokens)
                return

            choice = response_data.get("choices", [{}])[0]
            msg_obj = choice.get("message", {})
            content = msg_obj.get("content") or ""
            # Support reasoning models (DeepSeek, etc.)
            reasoning = msg_obj.get("reasoning_content") or ""
            if reasoning and not content:
                content = reasoning
            elif reasoning and content:
                content = f"> *Thinking: {reasoning.strip()}*\n\n{content}"

            tool_calls = msg_obj.get("tool_calls") or []
            # Normalize legacy function_call format if model used it
            if not tool_calls and msg_obj.get("function_call"):
                tool_calls = [{
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": msg_obj["function_call"]
                }]

            # Extract token usage
            usage = response_data.get("usage", {})
            total_prompt_tokens = usage.get("prompt_tokens", total_prompt_tokens)
            total_completion_tokens += usage.get("completion_tokens", 0)

            # Record assistant turn in context memory
            self.memory.add_message(sess_id, "assistant", content, tool_calls=tool_calls if tool_calls else None)

            # Stream text if model replied with commentary
            if content:
                for chunk in self._chunk_text(content):
                    await asyncio.sleep(0.008)
                    yield AgentText(chunk)

            # If no tool calls, the model has completed its response
            if not tool_calls:
                break

            # 4. Process Model Tool Calls
            for tool_call in tool_calls:
                fn = tool_call.get("function", {})
                fn_name = fn.get("name", "")
                call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}"

                try:
                    raw_args = fn.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}

                if fn_name == "run_command":
                    raw_cmd = args.get("command") or args.get("cmd") or args.get("cli") or args.get("input")
                    if raw_cmd is None and "commands" in args:
                        raw_cmd = args.get("commands")
                    if raw_cmd is None:
                        continue

                    # Support agents sending \r, \n, or \r\n (trailing, raw enter, or multi-line)
                    if isinstance(raw_cmd, list):
                        extracted_cmds = []
                        for item in raw_cmd:
                            item_norm = str(item).replace("\r\n", "\n").replace("\r", "\n")
                            for line in item_norm.split("\n"):
                                if line.strip():
                                    extracted_cmds.append(line.strip())
                        cmds_to_run = extracted_cmds if extracted_cmds else []
                    else:
                        norm_str = str(raw_cmd).replace("\r\n", "\n").replace("\r", "\n")
                        if not norm_str.strip() and norm_str:
                            cmds_to_run = ["\r\n"]
                        else:
                            split_lines = [line.strip() for line in norm_str.split("\n") if line.strip()]
                            cmds_to_run = split_lines if split_lines else []

                    if not cmds_to_run:
                        continue

                    dev_name = session.hostname if session else "disconnected"
                    outputs_for_context = []

                    for idx, cmd in enumerate(cmds_to_run):
                        sub_call_id = f"{call_id}_{idx}" if idx > 0 else call_id
                        disp_cmd = "<CR>" if cmd == "\r\n" else cmd

                        # Determine command risk
                        is_config = any(cmd.lower().startswith(w) for w in ("conf", "interface", "router", "ip route", "vlan", "no "))
                        if is_plan_mode and (is_config or is_mutating_cli_command(cmd)):
                            err_resp = "% Error: Configuration commands are disabled in Plan Mode. Inspect with read-only commands (show/get), formulate your plan, then type /build to execute."
                            yield AgentToolResult(sub_call_id, err_resp, False)
                            outputs_for_context.append(f"$ {disp_cmd}\n{err_resp}")
                            continue

                        risk = Risk.CONFIG if is_config else Risk.READ

                        # Yield tool call to UI
                        yield AgentToolCall(sub_call_id, dev_name, disp_cmd, risk)

                        # Execute real command on hardware
                        try:
                            res = await run_command_fn(cmd)
                            output = res.output if hasattr(res, "output") else str(res)
                            ok = res.ok if hasattr(res, "ok") else True
                        except Exception as err:
                            output = f"Command execution error: {err}"
                            ok = False

                        # Yield result to UI
                        yield AgentToolResult(sub_call_id, output, ok)
                        ctx_output = redact_sensitive_text(output) if getattr(settings, "redact_secrets", True) else output
                        outputs_for_context.append(f"$ {disp_cmd}\n{ctx_output}")

                    # Feed result back into conversation context
                    combined_output = "\n".join(outputs_for_context)
                    truncated_output = ContextMemoryManager.truncate_tool_output(combined_output)
                    self.memory.add_message(sess_id, "tool", truncated_output, tool_call_id=call_id)

                elif fn_name == "propose_change":
                    if is_plan_mode:
                        err_msg = "Error: propose_change is disabled in Plan Mode. Formulate the implementation plan in your response and instruct the user to type /build to execute."
                        self.memory.add_message(sess_id, "tool", err_msg, tool_call_id=call_id)
                        continue

                    title = args.get("title") or args.get("name") or "Proposed Change"
                    raw_cmds = args.get("commands") or args.get("cmds") or []
                    if not raw_cmds and args.get("command"):
                        raw_cmds = [args.get("command")]

                    commands = []
                    if isinstance(raw_cmds, str):
                        raw_norm = raw_cmds.replace("\r\n", "\n").replace("\r", "\n")
                        commands = [c.strip() for c in raw_norm.split("\n") if c.strip()]
                    elif isinstance(raw_cmds, list):
                        for c in raw_cmds:
                            c_norm = str(c).replace("\r\n", "\n").replace("\r", "\n")
                            for line in c_norm.split("\n"):
                                if line.strip():
                                    commands.append(line.strip())

                    rationale = args.get("rationale") or args.get("reason") or args.get("description") or ""
                    risk_str = str(args.get("risk") or "config").lower()
                    risk = Risk.DANGER if risk_str == "danger" else Risk.CONFIG
                    dev_name = session.hostname if session else "unassigned"

                    change = ProposedChange(
                        id=uuid.uuid4().hex[:8],
                        device=dev_name,
                        title=title,
                        commands=commands,
                        risk=risk,
                        rationale=rationale,
                    )

                    # Yield proposal to UI for operator approval
                    yield AgentProposal(change)

                    # Wait for human verdict
                    decision = await propose_change_fn(change)

                    if decision.approved:
                        exec_results = []
                        for idx, c in enumerate(decision.commands):
                            c_clean = c.strip()
                            if not c_clean:
                                continue
                            c_id = f"apply_{uuid.uuid4().hex[:6]}_{idx}"
                            yield AgentToolCall(c_id, dev_name, c_clean, Risk.CONFIG)
                            try:
                                res = await run_command_fn(c_clean)
                                output = res.output if hasattr(res, "output") else str(res)
                                ok = res.ok if hasattr(res, "ok") else True
                            except Exception as err:
                                output = f"Error: {err}"
                                ok = False
                            yield AgentToolResult(c_id, output, ok)
                            exec_results.append(f"{c_clean}: {'OK' if ok else 'ERROR'}")

                        if decision.save_after:
                            save_id = f"save_{uuid.uuid4().hex[:6]}"
                            yield AgentToolCall(save_id, dev_name, "write memory", Risk.CONFIG)
                            try:
                                res = await run_command_fn("write memory")
                                output = res.output if hasattr(res, "output") else str(res)
                                ok = res.ok if hasattr(res, "ok") else True
                            except Exception as err:
                                output = f"Error: {err}"
                                ok = False
                            yield AgentToolResult(save_id, output, ok)
                            exec_results.append("write memory: OK" if ok else "write memory: ERROR")

                        tail = " Configuration written to NVRAM (write memory)." if decision.save_after else " Modified in RAM only."
                        confirm_text = f"\n\n✓ **Applied {len(decision.commands)} commands to {dev_name} successfully.**{tail}"
                        for chunk in self._chunk_text(confirm_text):
                            await asyncio.sleep(0.006)
                            yield AgentText(chunk)

                        result_msg = f"Change approved and applied:\n" + "\n".join(exec_results)
                    else:
                        reject_text = "\n\nRemediation rejected by operator. Device running-config left unchanged."
                        for chunk in self._chunk_text(reject_text):
                            await asyncio.sleep(0.006)
                            yield AgentText(chunk)
                        result_msg = f"Proposal '{title}' was rejected by operator."

                    self.memory.add_message(sess_id, "tool", result_msg, tool_call_id=call_id)

        # Finished turn
        yield AgentDone(total_prompt_tokens, total_completion_tokens)

    async def _run_antigravity_turn(
        self,
        prompt: str,
        session: Optional[DeviceSession],
        provider: ProviderConfig,
        settings: Settings,
        agent_spec: Any,
        run_command_fn: Callable[[str], Coroutine[Any, Any, Any]],
        propose_change_fn: Callable[[ProposedChange], Coroutine[Any, Any, ApprovalDecision]],
        session_id: str,
        is_plan_mode: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        badge = getattr(agent_spec, "badge", "[ANTIGRAVITY]")
        model = provider.model or "gemini-3.8-flash-low"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        cid = self._agy_conversations.get(session_id)

        system_msg = self.build_system_message(session, agent_spec, is_plan_mode=is_plan_mode)
        if is_plan_mode:
            tool_inst = (
                f"### TOOL INSTRUCTIONS (PLAN MODE - STRICT READ-ONLY)\n"
                f"You have direct access to execute diagnostic commands on the connected hardware.\n"
                f"To run a read-only command, you MUST output ONLY a JSON object:\n"
                f'{{"tool_call": {{"name": "run_command", "arguments": {{"command": "<cli command>"}}}}}}\n'
                f"NOTE: Configuration changes (propose_change) and mutating commands are STRICTLY PROHIBITED in Plan Mode.\n"
                f"If diagnostic data is needed before formulating the plan, call run_command.\n"
                f"When answering the operator, provide your discovery questions and engineering plan in clean markdown without JSON."
            )
        else:
            tool_inst = (
                f"### TOOL INSTRUCTIONS\n"
                f"You have direct access to execute diagnostic commands on the connected hardware.\n"
                f"To run a command, you MUST output ONLY a JSON object:\n"
                f'{{"tool_call": {{"name": "run_command", "arguments": {{"command": "<cli command>"}}}}}}\n'
                f"To propose a configuration change, output ONLY a JSON object:\n"
                f'{{"tool_call": {{"name": "propose_change", "arguments": {{"title": "...", "commands": ["..."], "rationale": "...", "risk": "config"}}}}}}\n'
                f"If diagnostic data is needed before drawing conclusions, call run_command.\n"
                f"If answering the operator directly, provide your engineering analysis in clean markdown without JSON."
            )

        current_prompt = (
            f"{system_msg}\n\n"
            f"### OPERATOR REQUEST\n{prompt}\n\n"
            f"{tool_inst}"
        ) if not cid else prompt

        max_steps = getattr(settings, "max_agent_steps", 10) or 10

        effort = getattr(settings, "reasoning_effort", "medium") or "medium"
        if effort in ("off", "none"):
            effort = "low"
        elif effort == "max":
            effort = "xhigh"

        # Honor /effort first: auto change model variant to match configured effort
        m_pattern = re.compile(r'^(.*?)-(low|medium|high|xhigh|max)$', re.IGNORECASE)
        m_match = m_pattern.match(model)
        if m_match:
            base_model = m_match.group(1)
            if effort in ("off", "none", "low"):
                target_suffix = "low"
            elif effort in ("high", "max", "xhigh"):
                target_suffix = "high"
            else:
                target_suffix = "medium"
            new_model = f"{base_model}-{target_suffix}"
            if new_model.lower() != model.lower():
                model = new_model
                if provider:
                    provider.model = new_model

        agy_bin = ensure_agy_in_path(getattr(provider, "api_key", None))
        if not agy_bin:
            yield AgentError(
                f"{badge} Antigravity CLI ('agy') not found on system PATH or standard install directories. "
                f"Please ensure AGY CLI is installed and accessible."
            )
            yield AgentDone(total_prompt_tokens, total_completion_tokens)
            return

        for step in range(max_steps):
            cmd = [agy_bin, "--output-format", "json", "--effort", effort, "--model", model]
            if cid:
                cmd.extend(["--conversation", cid])
            cmd.extend(["-p", current_prompt])

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
            except Exception as exc:
                yield AgentError(f"{badge} Failed to launch AGY CLI: {exc}")
                yield AgentDone(total_prompt_tokens, total_completion_tokens)
                return

            if proc.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
                # If AGY CLI reported a conflict between --model and --effort:
                if ("conflicts with --effort" in err_text or "invalid model selection" in err_text) and m_pattern.match(model):
                    m_err = m_pattern.match(model)
                    if m_err:
                        base = m_err.group(1)
                        target = "low" if effort in ("off", "none", "low") else ("high" if effort in ("high", "max", "xhigh") else "medium")
                        model = f"{base}-{target}"
                        if provider:
                            provider.model = model
                        retry_cmd = [agy_bin, "--output-format", "json", "--effort", effort, "--model", model]
                        if cid:
                            retry_cmd.extend(["--conversation", cid])
                        retry_cmd.extend(["-p", current_prompt])
                        try:
                            retry_proc = await asyncio.create_subprocess_exec(
                                *retry_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            stdout, stderr = await retry_proc.communicate()
                            proc = retry_proc
                        except Exception:
                            pass
                if proc.returncode != 0:
                    err_text = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
                    yield AgentError(f"{badge} AGY CLI error: {err_text[:200]}")
                    yield AgentDone(total_prompt_tokens, total_completion_tokens)
                    return

            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
            except Exception:
                yield AgentError(f"{badge} Invalid JSON output from AGY CLI: {stdout.decode('utf-8', errors='replace')[:200]}")
                yield AgentDone(total_prompt_tokens, total_completion_tokens)
                return

            cid = data.get("conversation_id")
            if cid:
                self._agy_conversations[session_id] = cid

            resp_text = data.get("response", "")
            usage = data.get("usage", {})
            total_prompt_tokens = usage.get("input_tokens", total_prompt_tokens)
            total_completion_tokens += usage.get("output_tokens", 0)

            # Check for tool call in resp_text
            tc_obj = None
            try:
                parsed = json.loads(resp_text.strip())
                if isinstance(parsed, dict) and "tool_call" in parsed:
                    tc_obj = parsed["tool_call"]
            except Exception:
                m = re.search(r'```(?:json)?\s*(\{.*?"tool_call":\s*\{.*?\}.*?\})\s*```', resp_text, re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(1))
                        if isinstance(parsed, dict) and "tool_call" in parsed:
                            tc_obj = parsed["tool_call"]
                    except Exception:
                        pass

            if tc_obj:
                fn_name = tc_obj.get("name", "")
                args = tc_obj.get("arguments", {})
                call_id = f"call_{uuid.uuid4().hex[:8]}"

                if fn_name == "run_command":
                    raw_cmd = (args.get("command") or args.get("cmd") or args.get("cli") or args.get("input") or "")
                    if not raw_cmd and "commands" in args:
                        raw_cmd = args.get("commands")

                    if isinstance(raw_cmd, list):
                        extracted_cmds = []
                        for item in raw_cmd:
                            item_norm = str(item).replace("\r\n", "\n").replace("\r", "\n")
                            for line in item_norm.split("\n"):
                                if line.strip():
                                    extracted_cmds.append(line.strip())
                        cmds_to_run = extracted_cmds if extracted_cmds else []
                    else:
                        norm_str = str(raw_cmd).replace("\r\n", "\n").replace("\r", "\n")
                        if not norm_str.strip() and norm_str:
                            cmds_to_run = ["\r\n"]
                        else:
                            split_lines = [line.strip() for line in norm_str.split("\n") if line.strip()]
                            cmds_to_run = split_lines if split_lines else []

                    if not cmds_to_run:
                        continue

                    dev_name = session.hostname if session else "disconnected"
                    outputs_for_context = []

                    for idx, cli_cmd in enumerate(cmds_to_run):
                        sub_call_id = f"{call_id}_{idx}" if idx > 0 else call_id
                        disp_cmd = "<CR>" if cli_cmd == "\r\n" else cli_cmd

                        is_config = any(cli_cmd.lower().startswith(w) for w in ("conf", "interface", "router", "ip route", "vlan", "no "))
                        if is_plan_mode and (is_config or is_mutating_cli_command(cli_cmd)):
                            err_resp = "% Error: Configuration commands are disabled in Plan Mode. Inspect with read-only commands (show/get), formulate your plan, then type /build to execute."
                            yield AgentToolResult(sub_call_id, err_resp, False)
                            outputs_for_context.append(f"$ {disp_cmd}\n{err_resp}")
                            continue

                        risk = Risk.CONFIG if is_config else Risk.READ

                        yield AgentToolCall(sub_call_id, dev_name, disp_cmd, risk)

                        try:
                            res = await run_command_fn(cli_cmd)
                            output = res.output if hasattr(res, "output") else str(res)
                            ok = res.ok if hasattr(res, "ok") else True
                        except Exception as err:
                            output = f"Command execution error: {err}"
                            ok = False

                        yield AgentToolResult(sub_call_id, output, ok)
                        ctx_output = redact_sensitive_text(output) if getattr(settings, "redact_secrets", True) else output
                        outputs_for_context.append(f"$ {disp_cmd}\n{ctx_output}")

                    combined_output = "\n".join(outputs_for_context)
                    truncated_output = ContextMemoryManager.truncate_tool_output(combined_output)
                    current_prompt = (
                        f"Result of execution on {dev_name}:\n```\n{truncated_output}\n```\n"
                        f"If more diagnostic commands are required, call run_command. Otherwise, provide your analysis to the operator in markdown."
                    )
                    continue

                elif fn_name == "propose_change":
                    if is_plan_mode:
                        err_msg = "Error: propose_change is disabled in Plan Mode. Formulate the implementation plan in your response and instruct the user to type /build to execute."
                        current_prompt = f"Tool Error: {err_msg}\nProceed with your plan."
                        continue

                    title = args.get("title") or "Proposed Change"
                    raw_cmds = args.get("commands") or args.get("cmds") or []
                    if not raw_cmds and args.get("command"):
                        raw_cmds = [args.get("command")]

                    commands = []
                    if isinstance(raw_cmds, str):
                        raw_norm = raw_cmds.replace("\r\n", "\n").replace("\r", "\n")
                        commands = [c.strip() for c in raw_norm.split("\n") if c.strip()]
                    elif isinstance(raw_cmds, list):
                        for c in raw_cmds:
                            c_norm = str(c).replace("\r\n", "\n").replace("\r", "\n")
                            for line in c_norm.split("\n"):
                                if line.strip():
                                    commands.append(line.strip())

                    rationale = args.get("rationale") or ""
                    risk_str = str(args.get("risk") or "config").lower()
                    risk = Risk.DANGER if risk_str == "danger" else Risk.CONFIG
                    dev_name = session.hostname if session else "unassigned"

                    change = ProposedChange(
                        id=uuid.uuid4().hex[:8],
                        device=dev_name,
                        title=title,
                        commands=commands,
                        risk=risk,
                        rationale=rationale,
                    )

                    yield AgentProposal(change)
                    decision = await propose_change_fn(change)

                    if decision.approved:
                        exec_results = []
                        for idx, c in enumerate(decision.commands):
                            c_clean = c.strip()
                            if not c_clean:
                                continue
                            c_id = f"apply_{uuid.uuid4().hex[:6]}_{idx}"
                            yield AgentToolCall(c_id, dev_name, c_clean, Risk.CONFIG)
                            try:
                                res = await run_command_fn(c_clean)
                                output = res.output if hasattr(res, "output") else str(res)
                                ok = res.ok if hasattr(res, "ok") else True
                            except Exception as err:
                                output = f"Error: {err}"
                                ok = False
                            yield AgentToolResult(c_id, output, ok)
                            exec_results.append(f"{c_clean}: {'OK' if ok else 'ERROR'}")

                        if decision.save_after:
                            save_id = f"save_{uuid.uuid4().hex[:6]}"
                            yield AgentToolCall(save_id, dev_name, "write memory", Risk.CONFIG)
                            try:
                                res = await run_command_fn("write memory")
                                output = res.output if hasattr(res, "output") else str(res)
                                ok = res.ok if hasattr(res, "ok") else True
                            except Exception as err:
                                output = f"Error: {err}"
                                ok = False
                            yield AgentToolResult(save_id, output, ok)
                            exec_results.append("write memory: OK" if ok else "write memory: ERROR")

                        tail = " Configuration written to NVRAM (write memory)." if decision.save_after else " Modified in RAM only."
                        confirm_text = f"\n\n✓ **Applied {len(decision.commands)} commands to {dev_name} successfully.**{tail}"
                        for chunk in self._chunk_text(confirm_text):
                            await asyncio.sleep(0.006)
                            yield AgentText(chunk)

                        result_msg = f"Change approved and applied:\n" + "\n".join(exec_results)
                    else:
                        reject_text = "\n\nRemediation rejected by operator. Device running-config left unchanged."
                        for chunk in self._chunk_text(reject_text):
                            await asyncio.sleep(0.006)
                            yield AgentText(chunk)
                        result_msg = f"Proposal '{title}' was rejected by operator."

                    current_prompt = f"Change proposal status:\n{result_msg}\nProceed with summary."
                    continue

            # No tool call; stream assistant markdown
            for chunk in self._chunk_text(resp_text):
                await asyncio.sleep(0.008)
                yield AgentText(chunk)
            break

        yield AgentDone(total_prompt_tokens, total_completion_tokens)

    def _build_request(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        settings: Optional[Settings] = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Format request URL, headers, and payload according to API scheme."""
        endpoint, headers, payload, _ = build_llm_request(provider, messages, tools=tools, settings=settings)
        return endpoint, headers, payload

    def _format_api_error(self, status: int, body: str, provider: ProviderConfig) -> str:
        """Format API errors into helpful actionable guidance."""
        try:
            d = json.loads(body)
            msg = d.get("error", {}).get("message") or d.get("message") or body
        except Exception:
            msg = body

        if status == 401:
            return f"Authentication failed (401) on {provider.name}. Verify your API key or token in Model settings (F2)."
        if status == 402:
            return f"Insufficient credits (402) on {provider.name} for model '{provider.model}'. Switch to a free/lower tier model in F2 or top up credits."
        if status == 403:
            return (
                f"Access forbidden (403) on {provider.name}: {msg}\n"
                f"If using Google Antigravity, note that its local OAuth token is scoped for Cloud Platform / AI Studio. "
                f"Press F2 to select OpenRouter (claude-sonnet-5.5) which is verified active on your system."
            )
        if status == 404:
            return f"Model '{provider.model}' was not found on {provider.name} (404). Please select an active model in F2."
        if status == 429:
            return f"Rate limit exceeded (429) on {provider.name}. Please wait a moment before sending another request."
        return f"HTTP {status} from {provider.name}: {msg}"

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 12) -> list[str]:
        words = text.split(" ")
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunks.append(" ".join(words[i:i + chunk_size]) + " ")
        return chunks

    def clear_session(self, session_id: str) -> None:
        """Clear session memory and active AGY conversation state."""
        self.memory.clear(session_id)
        self._agy_conversations.pop(session_id, None)

    def set_session_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Set context memory messages for a session."""
        self.memory.set_messages(session_id, messages)

    def clone_session(self, source_session_id: str, target_session_id: str) -> None:
        """Clone context memory for a session into a new session."""
        self.memory.clone_session(source_session_id, target_session_id)

    async def call_llm_stateless(
        self,
        provider: ProviderConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2500,
    ) -> Optional[str]:
        """Execute a single-turn stateless completion for background tasks (titling, review, compaction)."""
        if provider.scheme == ApiScheme.ANTIGRAVITY or provider.spec_id == "antigravity" or provider.base_url == "local:agy" or "antigravity" in provider.name.lower():
            agy_bin = ensure_agy_in_path(getattr(provider, "api_key", None))
            if not agy_bin:
                logger.error("AGY call_llm_stateless: agy executable not found")
                return None
            model = provider.model or "gemini-3.8-flash-low"
            stateless_effort = "low"
            m_stateless = re.match(r'^(.*?)-(low|medium|high|xhigh|max)$', model, re.IGNORECASE)
            if m_stateless:
                model = f"{m_stateless.group(1)}-low"
            full_prompt = f"{system_prompt}\n\n{user_prompt}"
            cmd = [agy_bin, "--output-format", "json", "--effort", stateless_effort, "--model", model, "-p", full_prompt]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
                if proc.returncode == 0:
                    data = json.loads(stdout.decode("utf-8", errors="replace"))
                    return data.get("response", "")
                else:
                    err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
                    logger.error("AGY call_llm_stateless error: %s", err[:200])
                    return None
            except Exception as exc:
                logger.error("AGY call_llm_stateless exception: %s", exc)
                return None

        api_key = provider.api_key.strip() if isinstance(provider.api_key, str) else ""
        if not api_key and "localhost" not in provider.base_url and "127.0.0.1" not in provider.base_url:
            raise ValueError(f"No API key configured for {provider.name}. Configure an API key or switch providers with F2.")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        endpoint, headers, payload = self._build_request(provider, messages, tools=None)
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens
        try:
            resp = await self._http_client.post(endpoint, headers=headers, json=payload, timeout=60.0)
            if resp.status_code == 200:
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                msg_obj = choice.get("message", {})
                content = msg_obj.get("content") or ""
                reasoning = msg_obj.get("reasoning_content") or ""
                if not content and reasoning:
                    content = reasoning
                return content
            else:
                err_text = self._format_api_error(resp.status_code, resp.text, provider)
                logger.error("HTTP call_llm_stateless API error: %s", err_text)
                return None
        except Exception as exc:
            logger.error("HTTP call_llm_stateless exception: %s", exc)
            return None

    @staticmethod
    def _format_messages_for_compaction(messages: list[dict[str, Any]], fallback_transcript: str = "") -> str:
        lines: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "")).upper()
            content = str(msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls")
            if role == "SYSTEM":
                continue
            if role == "USER":
                if "[Session Compaction Checkpoint" in content:
                    lines.append(f"PREVIOUS CHECKPOINT:\n{content}")
                else:
                    lines.append(f"USER: {content}")
            elif role == "ASSISTANT":
                if tool_calls:
                    call_strs = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        name = fn.get("name", "tool")
                        args = fn.get("arguments", "")
                        call_strs.append(f"{name}({args})")
                    desc = f"ASSISTANT (Executed commands: {', '.join(call_strs)})"
                    if content:
                        desc += f"\n{content}"
                    lines.append(desc)
                elif content:
                    lines.append(f"ASSISTANT: {content}")
            elif role == "TOOL":
                tool_name = msg.get("name") or "tool"
                truncated = ContextMemoryManager.truncate_tool_output(content, max_lines=60)
                lines.append(f"COMMAND OUTPUT ({tool_name}):\n{truncated}")

        formatted = "\n\n".join(lines).strip()
        if not formatted and fallback_transcript:
            return fallback_transcript.strip()
        return formatted

    async def compact_session(
        self,
        session_id: str,
        provider: ProviderConfig,
        session: Optional[DeviceSession] = None,
        agent_spec: Optional[Any] = None,
        is_plan_mode: bool = False,
        fallback_transcript: str = "",
    ) -> str:
        """Compact conversation history into a structured checkpoint summary and reset memory starting point."""
        raw_msgs = self.memory.get_messages(session_id)
        transcript = self._format_messages_for_compaction(raw_msgs, fallback_transcript)
        if not transcript.strip():
            transcript = "No prior user interaction or command history recorded in this session."

        hw_info = ""
        if session:
            hw_info = (
                f"CONNECTED HARDWARE TELEMETRY:\n"
                f"- Hostname: {session.hostname}\n"
                f"- Vendor/Platform: {session.profile.vendor.value.upper()}\n"
                f"- Transport: {session.profile.transport.value.upper()}\n"
                f"- Prompt: {session.prompt}\n\n"
            )

        compaction_system_prompt = (
            "You are an expert network engineering assistant compacting conversation context into a structured, high-density session checkpoint.\n"
            "Analyze the complete conversation history and generate a comprehensive overview and session checkpoint.\n"
            "Preserve all critical technical facts, hardware models, IP addresses, VLANs, interfaces, diagnostic findings, and operator objectives.\n"
            "Do not include any emojis.\n\n"
            "Format your output in clean Markdown using the following sections:\n"
            "### Session Objective & Scope\n"
            "- Detail the primary goal, requested network tasks, and operational context.\n\n"
            "### Connected Devices & Discovered State\n"
            "- Target hostnames, models, vendors, OS versions, IP/interfaces, and hardware state discovered.\n\n"
            "### Actions Performed & Diagnostic Findings\n"
            "- Key CLI commands executed, telemetry analyzed, and findings discovered.\n\n"
            "### Configuration & Architectural Decisions\n"
            "- Parameters established (VLANs, IPs, routing policies, safety guardrails).\n\n"
            "### Current Status & Remaining Next Steps\n"
            "- The exact current state of the task, what is finished, and pending next steps.\n"
        )

        user_prompt = (
            f"{hw_info}"
            f"Here is the conversation history to compact:\n\n"
            f"{transcript}"
        )

        summary = await self.call_llm_stateless(
            provider=provider,
            system_prompt=compaction_system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=2500,
        )

        if not summary or not summary.strip():
            raise RuntimeError(f"Compaction failed: Provider {provider.name} returned an empty summary.")

        summary_text = summary.strip()

        # Clear old session history from memory and AGY state
        self.clear_session(session_id)

        # Seed context memory with the new starting point
        sys_msg = self.build_system_message(session, agent_spec, is_plan_mode=is_plan_mode)
        self.memory.add_message(session_id, "system", sys_msg)
        self.memory.add_message(
            session_id,
            "user",
            f"[Session Compaction Checkpoint - Conversation history has been compacted]\n\n{summary_text}\n\nPlease continue the session from this checkpoint."
        )
        self.memory.add_message(
            session_id,
            "assistant",
            "Understood. I have loaded the conversation checkpoint and all discovered hardware state into my active context. How would you like to proceed?"
        )

        return summary_text
