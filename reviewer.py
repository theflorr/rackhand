from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from models import ProposedChange, ProviderConfig, Risk


@dataclass
class ReviewVerdict:
    auto_approve: bool
    is_dangerous: bool
    risk: Risk
    reason: str
    rollback: list[str] = field(default_factory=list)
    touched_config: str = ""


AUTONOMOUS_REVIEWER_PROMPT = """You are an independent, zero-context Network Change Reviewer and Safety Auditor.
You examine proposed network configuration commands against the target device and its current configuration snippet.
You have no prior conversation history, no bias, and no assumptions.

Target Device: {hostname} ({platform})
Current Configuration of Touched Areas:
{touched_config}

Proposed Commands to Apply:
{commands}

Agent's Stated Rationale:
{rationale}

Your Goal:
1. Examine what these commands actually change on this specific network operating system.
2. Review the touched current config (if provided).
3. Decide whether to AUTOMATICALLY APPROVE this change autonomously, or ASK FOR HUMAN OPERATOR APPROVAL.

Decision Guidelines:
- AUTO-APPROVE (decision = "auto_approve"):
  Routine, non-destructive, safe operational additions or minor edits. For example:
  - Adding or updating interface descriptions or comments
  - Assigning an access port to an access VLAN
  - Configuring or tweaking NTP, DNS, syslog, or SNMP servers
  - Adding non-conflicting static routes
  - Creating new local VLANs that do not prune or drop existing traffic
  - Standard non-impacting diagnostic logging or monitoring options

- ASK FOR APPROVAL (decision = "ask_approval"):
  Any change with potential for disruption, ambiguity, or significant blast radius. For example:
  - Modifying trunk ports, allowed VLAN lists, uplinks, or port-channel/LACP configs
  - Changing or deleting management IP addresses, default gateways, or SSH/AAA settings
  - Modifying dynamic routing protocols (BGP, OSPF, EIGRP, IS-IS) or route-maps
  - Modifies or deletes ACLs, firewall filters, or security policies
  - Uses 'shutdown' on an interface or affects an active uplink
  - Erases, resets, or replaces significant configuration blocks
  - Commands with risk of loop creation, traffic blackholing, or connectivity loss

Always generate the exact rollback commands to undo the proposed changes.

Respond ONLY with valid JSON in this exact structure:
{{
  "decision": "auto_approve" | "ask_approval",
  "risk": "config" | "danger",
  "reason": "1-2 sentence concise explanation of why it can be auto-approved or why human review is required",
  "rollback": ["command 1", "command 2"]
}}
"""


DANGER_EVALUATOR_PROMPT = """You are a specialized Network Safety Gatekeeper checking exclusively for CATASTROPHIC or DESTRUCTIVE DANGEROUS actions.
You have no prior conversation history and no bias.

Target Device: {hostname} ({platform})
Proposed Commands to Apply:
{commands}

Agent's Stated Rationale:
{rationale}

Your Goal:
Evaluate whether any of these commands represent a CATASTROPHIC or HIGH-RISK action that could cause severe outages or loss of device control.

Guidelines:
- BE FORGIVING to standard, routine, and non-destructive configuration changes (e.g. creating VLANs, changing port settings, normal routing policies, IP addresses, standard ACLs, interface descriptions). These are NOT catastrophic and MUST be classified as is_dangerous = false.
- ONLY classify as is_dangerous = true if commands cause severe operational destruction or permanent lockout:
  - Device reload, reboot, power-off, system restart
  - Erasing, zeroing, or wiping startup-config or bootflash (e.g. write erase, erase startup-config)
  - Formatting flash filesystems, deleting boot images or system kernels
  - Shutting down active management interfaces or primary uplinks
  - Deleting entire dynamic routing processes (e.g. 'no router bgp', 'no router ospf')
  - Factory reset commands

Always generate the exact rollback commands if applicable.

Respond ONLY with valid JSON in this exact structure:
{{
  "is_dangerous": true | false,
  "risk": "danger" | "config",
  "reason": "1-2 sentence concise explanation of why this is considered dangerous or why it is safe",
  "rollback": ["command 1", "command 2"]
}}
"""


def extract_touched_targets(commands: list[str]) -> list[str]:
    """Identify interfaces, routing blocks, vlan blocks, etc. touched by commands."""
    targets: list[str] = []
    for cmd in commands:
        cmd_clean = cmd.strip()
        # Interface
        m = re.match(r"^(?:default\s+)?interface\s+([a-zA-Z0-9\/\.\-]+)", cmd_clean, re.IGNORECASE)
        if m:
            t = f"interface {m.group(1)}"
            if t not in targets:
                targets.append(t)
            continue
        # Router (bgp, ospf, eigrp, rip, isis)
        m = re.match(r"^(?:no\s+)?router\s+([a-zA-Z0-9\-]+(?:\s+[a-zA-Z0-9\-]+)?)", cmd_clean, re.IGNORECASE)
        if m:
            t = f"router {m.group(1)}"
            if t not in targets:
                targets.append(t)
            continue
        # VLAN
        m = re.match(r"^(?:no\s+)?vlan\s+(\d+(?:-\d+)?)", cmd_clean, re.IGNORECASE)
        if m:
            t = f"vlan {m.group(1)}"
            if t not in targets:
                targets.append(t)
            continue
        # Access-list / Route-map
        m = re.match(r"^(?:ip\s+)?(?:access-list|prefix-list|route-map)\s+([a-zA-Z0-9\-_]+)", cmd_clean, re.IGNORECASE)
        if m:
            t = m.group(0)
            if t not in targets:
                targets.append(t)
            continue
    return targets


async def fetch_touched_config(
    session: Any,
    targets: list[str],
    run_command_fn: Optional[Callable[[str], Coroutine[Any, Any, Any]]] = None,
) -> str:
    """Fetch current running-config snippets for touched targets."""
    if not targets or not run_command_fn or not session:
        return "(No live target configuration available)"

    snippets = []
    for target in targets[:3]:
        if target.startswith("interface"):
            q_cmd = f"show running-config {target}"
        elif target.startswith("router") or target.startswith("vlan") or "access-list" in target or "route-map" in target:
            q_cmd = f"show running-config | section {target}"
        else:
            q_cmd = f"show running-config {target}"

        try:
            res = await run_command_fn(q_cmd)
            out = res.output if hasattr(res, "output") else str(res)
            if out and "%" not in out[:20] and "error" not in out[:20].lower():
                clean_lines = [
                    ln for ln in out.splitlines()
                    if ln.strip() and not ln.strip().startswith("Building configuration")
                ]
                if clean_lines:
                    snippet_text = "\n".join(clean_lines[:35])
                    try:
                        from llm_engine import redact_sensitive_text
                        snippet_text = redact_sensitive_text(snippet_text)
                    except Exception:
                        pass
                    snippets.append(f"# Current config for [{target}]:\n" + snippet_text)
        except Exception:
            pass

    return "\n\n".join(snippets) if snippets else "(Current configuration section not found or clean)"


def _parse_reviewer_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _get_session_platform(session: Any) -> str:
    if session is None:
        return "cisco_ios"
    v = getattr(session, "vendor", None) or getattr(getattr(session, "profile", None), "vendor", None)
    if v is not None:
        return v.value if hasattr(v, "value") else str(v)
    return getattr(session, "platform", None) or "cisco_ios"


async def review_change_autonomous(
    change: ProposedChange,
    session: Any,
    provider: ProviderConfig,
    llm_engine: Any,
    run_command_fn: Optional[Callable[[str], Coroutine[Any, Any, Any]]] = None,
) -> ReviewVerdict:
    """Spawns a fresh agent to inspect touched config, evaluate commands, and decide auto-approval."""
    hostname = session.hostname if session else change.device or "Device"
    platform = _get_session_platform(session)
    cmds_str = "\n".join(change.commands)

    # 1. Inspect and fetch touched current configuration
    targets = extract_touched_targets(change.commands)
    touched_cfg = await fetch_touched_config(session, targets, run_command_fn)

    # 2. Render prompt
    prompt = AUTONOMOUS_REVIEWER_PROMPT.format(
        hostname=hostname,
        platform=platform,
        touched_config=touched_cfg,
        commands=cmds_str,
        rationale=change.rationale or "Not specified",
    )

    try:
        raw_resp = await llm_engine.call_llm_stateless(
            provider=provider,
            system_prompt="You are an autonomous network safety reviewer. Reply in JSON only.",
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=600,
        )
        if not raw_resp:
            raise ValueError("Empty response from reviewer LLM")
        data = _parse_reviewer_json(raw_resp)
        decision = str(data.get("decision", "ask_approval")).lower().strip()
        risk_str = str(data.get("risk", "config")).lower().strip()
        risk = Risk.DANGER if risk_str == "danger" else Risk.CONFIG
        reason = str(data.get("reason", "")).strip() or "Evaluated by Autonomous Reviewer Agent."
        raw_rollback = data.get("rollback", [])
        rollback = [str(r).strip() for r in raw_rollback if str(r).strip()] if isinstance(raw_rollback, list) else []

        auto_approve = (decision == "auto_approve" and risk is not Risk.DANGER)
        return ReviewVerdict(
            auto_approve=auto_approve,
            is_dangerous=(risk is Risk.DANGER),
            risk=risk,
            reason=reason,
            rollback=rollback,
            touched_config=touched_cfg,
        )
    except Exception as exc:
        # Fallback safely: if reviewer call fails, do NOT auto-approve; ask human operator
        return ReviewVerdict(
            auto_approve=False,
            is_dangerous=False,
            risk=Risk.CONFIG,
            reason=f"Autonomous review fallback (API error: {exc}); requesting operator approval.",
            rollback=[],
            touched_config=touched_cfg,
        )


async def review_danger_only(
    change: ProposedChange,
    session: Any,
    provider: ProviderConfig,
    llm_engine: Any,
) -> ReviewVerdict:
    """Spawns a fresh agent specifically checking for catastrophic/dangerous actions."""
    hostname = session.hostname if session else change.device or "Device"
    platform = _get_session_platform(session)
    cmds_str = "\n".join(change.commands)

    prompt = DANGER_EVALUATOR_PROMPT.format(
        hostname=hostname,
        platform=platform,
        commands=cmds_str,
        rationale=change.rationale or "Not specified",
    )

    try:
        raw_resp = await llm_engine.call_llm_stateless(
            provider=provider,
            system_prompt="You are a network danger gatekeeper. Reply in JSON only.",
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=500,
        )
        if not raw_resp:
            raise ValueError("Empty response from danger gatekeeper LLM")
        data = _parse_reviewer_json(raw_resp)
        is_dang = bool(data.get("is_dangerous", False))
        risk_str = str(data.get("risk", "config" if not is_dang else "danger")).lower().strip()
        risk = Risk.DANGER if (is_dang or risk_str == "danger") else Risk.CONFIG
        reason = str(data.get("reason", "")).strip() or ("Dangerous action flagged." if risk is Risk.DANGER else "Safe change passed.")
        raw_rollback = data.get("rollback", [])
        rollback = [str(r).strip() for r in raw_rollback if str(r).strip()] if isinstance(raw_rollback, list) else []

        return ReviewVerdict(
            auto_approve=not (risk is Risk.DANGER),
            is_dangerous=(risk is Risk.DANGER),
            risk=risk,
            reason=reason,
            rollback=rollback,
        )
    except Exception as exc:
        # Fail-closed on API/parse failure: never auto-approve without a verified danger check
        danger_keywords = ("reload", "reboot", "erase", "format", "write erase", "factory-reset", "shutdown", "no router")
        has_danger = any(any(kw in cmd.lower() for kw in danger_keywords) for cmd in change.commands)
        risk = Risk.DANGER if has_danger else Risk.CONFIG
        return ReviewVerdict(
            auto_approve=False,
            is_dangerous=has_danger,
            risk=risk,
            reason=f"Danger gatekeeper unavailable ({exc}); requesting operator approval.",
            rollback=[],
        )
