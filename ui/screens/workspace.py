from __future__ import annotations

import asyncio
import copy
import re
import uuid
from typing import Any, Optional

from rich import box
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button, Checkbox, Input, Label, OptionList, RichLog, Select, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from backend import Backend
from models import (
    AgentDone, AgentError, AgentProposal, AgentText, AgentToolCall, AgentToolResult,
    ApprovalDecision, ChatSession, DeviceSession, ProposedChange, ProviderConfig,
    Risk, Settings, Transport, Vendor, extract_hostname,
)
from ui.messages import (
    AssistantMessage, ChatMessage, CompactionCheckpointMessage, GenericMessage,
    ProposalVerdictMessage, ThemedMarkdown, ToolBundleMessage, ToolCallEntry, UserMessage,
)
from ui.suggestions import CommandSuggestionsBox, PromptInput
from agents import (
    AGENTS, get_agent, get_agent_options, resolve_agent,
    to_plan_agent, to_build_agent, is_plan_agent,
)
from ui.modals import (
    AgentPickerModal, ApprovalModal, ConnectScreen, ProvidersScreen, SettingsModal,
    UsageModal,
)
from ui.styles import (
    MenuStatic, SelectableStatic, build_sidebar_banner, fmt_bytes_prompt,
    highlight_config, pill, sel_value,
)

THEMES = ["rackhand", "tokyo-night", "nord", "gruvbox", "dracula", "catppuccin-mocha", "textual-dark"]

SHORT_OS: dict[Vendor, str] = {
    Vendor.AUTO: "Auto",
    Vendor.CISCO_IOS: "IOS-XE",
    Vendor.CISCO_NXOS: "NX-OS",
    Vendor.CISCO_ASA: "ASA",
    Vendor.FORTIOS: "FortiOS",
    Vendor.JUNIPER_JUNOS: "Junos",
    Vendor.ARISTA_EOS: "EOS",
    Vendor.VYOS: "VyOS",
    Vendor.LINUX: "Linux",
    Vendor.GENERIC: "CLI",
}


def format_token_count(n: int) -> str:
    """Format token count with k/M suffix like Claude Code (e.g. 1.2k, 200k, 1M)."""
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.1f}M" if (val * 10) % 10 != 0 else f"{int(val)}M"
    if n >= 10_000:
        val = n / 1_000
        return f"{int(val)}k"
    if n >= 1_000:
        val = n / 1_000
        return f"{val:.1f}k" if (val * 10) % 10 != 0 else f"{int(val)}k"
    return str(n)


def get_model_context_window(prov: Optional[ProviderConfig]) -> int:
    """Return model's context window limit in tokens."""
    if prov:
        if prov.context_window and prov.context_window > 0:
            return prov.context_window
        model_str = (prov.model or "").lower()
        spec_str = (prov.spec_id or "").lower()
        if "gemini" in model_str or "gemini" in spec_str:
            if "pro" in model_str:
                return 2_000_000
            return 1_000_000
        if "claude" in model_str or "anthropic" in spec_str:
            return 200_000
        if any(k in model_str for k in ("gpt-4", "o1", "o3", "openai")) or "openai" in spec_str:
            return 128_000
        if "deepseek" in model_str or "deepseek" in spec_str:
            return 128_000
        if "llama" in model_str or "nvidia" in spec_str or "groq" in spec_str:
            return 128_000
    return 200_000


class SessionRow(Horizontal):
    """A row in the sidebar session list showing the session title and a hoverable delete button."""
    can_focus = False

    class Selected(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class Deleted(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, session_id: str, title: str, is_active: bool) -> None:
        classes = "session-row active" if is_active else "session-row"
        super().__init__(classes=classes)
        self.session_id = session_id
        self.title_text = title
        self.is_active = is_active

    def compose(self) -> ComposeResult:
        title = self.title_text
        if len(title) > 18:
            title = title[:17] + "…"
        t = Text(title, style="bold white" if self.is_active else "#999999")
        yield Static(t, classes="session-title")
        btn = Button("×", classes="session-del-btn", tooltip="Delete session")
        btn.can_focus = False
        yield btn

    def on_enter(self, event: events.Enter) -> None:
        self.set_class(True, "hovered")

    def on_leave(self, event: events.Leave) -> None:
        if not self.is_mouse_over:
            self.set_class(False, "hovered")

    def on_click(self, event: events.Click) -> None:
        if isinstance(event.widget, Button) or (event.widget and "session-del-btn" in getattr(event.widget, "classes", [])):
            return
        self.post_message(self.Selected(self.session_id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("session-del-btn"):
            event.stop()
            self.post_message(self.Deleted(self.session_id))


class SidebarDivider(Static):
    """Thin interactive strip that tells the WorkspaceScreen to start a drag."""

    SIDEBAR_MIN = 16
    SIDEBAR_MAX = 60

    def __init__(self) -> None:
        super().__init__("", id="sidebar-divider")

    def render(self) -> Text:
        h = max(self.size.height, 1)
        return Text("\n".join(["│"] * h), no_wrap=True)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.screen._start_sidebar_drag(event.screen_x)
        event.stop()


COMMON_NET_COMMANDS = [
    "show running-config",
    "show ip interface brief",
    "show interfaces status",
    "show version",
    "show vlan brief",
    "show cdp neighbors",
    "show lldp neighbors",
    "show ip route",
    "show inventory",
    "show environment",
    "configure terminal",
    "write memory",
    "terminal length 0",
    "ping ",
    "traceroute ",
    "exit",
    "end",
]


class ConsoleInput(Input):
    """Raw network terminal input with command history and CLI completions."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.history: list[str] = []
        self.history_index: int = -1
        self._current_buffer: str = ""

    def add_to_history(self, cmd: str) -> None:
        c = cmd.strip()
        if c:
            if not self.history or self.history[-1] != c:
                self.history.append(c)
        self.history_index = -1
        self._current_buffer = ""

    def on_key(self, event: events.Key) -> None:
        if event.key == "up":
            event.stop()
            event.prevent_default()
            if not self.history:
                return
            if self.history_index == -1:
                self._current_buffer = self.value
                self.history_index = len(self.history) - 1
            elif self.history_index > 0:
                self.history_index -= 1
            self.value = self.history[self.history_index]
            self.cursor_position = len(self.value)
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            if self.history_index == -1:
                return
            if self.history_index < len(self.history) - 1:
                self.history_index += 1
                self.value = self.history[self.history_index]
            else:
                self.history_index = -1
                self.value = self._current_buffer
            self.cursor_position = len(self.value)
        elif event.key in ("escape", "ctrl+u"):
            event.stop()
            event.prevent_default()
            self.value = ""
            self.history_index = -1
        elif event.key == "tab":
            event.stop()
            event.prevent_default()
            val = self.value.strip().lower()
            if not val:
                self.value = "show "
                self.cursor_position = len(self.value)
                return
            matches = [cmd for cmd in COMMON_NET_COMMANDS if cmd.lower().startswith(val)]
            if matches:
                self.value = matches[0]
                self.cursor_position = len(self.value)


class WorkspaceScreen(Screen):
    """Main working view: devices on the left; Agent / Console / Changes tabs on the right."""
    BINDINGS = [
        Binding("ctrl+n", "add_device", "Connect"),
        Binding("f2", "model", "Model"),
        Binding("f3", "settings", "Settings"),
        Binding("ctrl+d", "disconnect", "Disconnect"),
        Binding("ctrl+l", "clear_chat", "Clear"),
        Binding("f4", "focus_prompt", "Focus"),
        Binding("ctrl+q", "app.quit", "Quit"),
    ]

    # ── Sidebar drag state ─────────────────────────────────────────────────────
    _drag_active: bool = False
    _drag_start_x: int = 0
    _drag_sidebar_w: int = 25

    def __init__(self) -> None:
        super().__init__()
        self.buffers: dict[str, list[Text]] = {}
        self.pending: dict[str, ProposedChange] = {}
        self.tokens_in = 0
        self.tokens_out = 0
        self._busy = False
        self.chat_sessions: list[ChatSession] = []
        self.active_session_id: Optional[str] = None
        self.chat_messages: dict[str, list[ChatMessage]] = {}
        self._session_counter: int = 0
        self.active_agent_id: str = "auto"
        self.current_mode: str = "build"

    def _start_sidebar_drag(self, screen_x: int) -> None:
        self._drag_active = True
        self._drag_start_x = screen_x
        try:
            self._drag_sidebar_w = self.query_one("#sidebar").size.width
        except Exception:
            self._drag_sidebar_w = 25
        self.query_one("#sidebar-divider").add_class("dragging")

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._drag_active:
            return
        delta = event.screen_x - self._drag_start_x
        new_w = max(SidebarDivider.SIDEBAR_MIN, min(SidebarDivider.SIDEBAR_MAX,
                                                     self._drag_sidebar_w + delta))
        try:
            self.query_one("#sidebar").styles.width = new_w
        except Exception:
            pass

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_active:
            self._drag_active = False
            try:
                self.query_one("#sidebar-divider").remove_class("dragging")
            except Exception:
                pass

    @property
    def chat_histories(self) -> dict[str, list[tuple[Any, str]]]:
        out: dict[str, list[tuple[Any, str]]] = {}
        for sid, msgs in self.chat_messages.items():
            out[sid] = [(m.render(), m.css_class) for m in msgs]
        return out

    @property
    def active_chat_session(self) -> Optional[ChatSession]:
        return next((s for s in self.chat_sessions if s.id == self.active_session_id), None)

    def create_chat_session(self) -> ChatSession:
        self._session_counter += 1
        sess = ChatSession(
            id=uuid.uuid4().hex[:8],
            num=self._session_counter,
            mode=self.current_mode,
        )
        self.chat_sessions.append(sess)
        self.chat_messages[sess.id] = []
        return sess

    def fork_chat_session(self, source_session_id: str) -> ChatSession:
        """Clone an existing session's messages and state into a new session titled '{title} (fork #N)'."""
        source_sess = next((s for s in self.chat_sessions if s.id == source_session_id), None)
        cur_title = (source_sess.title if source_sess and source_sess.title else None) or (source_sess.display_title if source_sess else "Session")

        # Determine fork title
        match = re.search(r"^(.*?)(?:\s*\(fork\s*#\d+\))?$", cur_title)
        base_title = match.group(1).strip() if match else cur_title

        existing_nums: list[int] = []
        pattern = re.compile(rf"^{re.escape(base_title)}\s*\(fork\s*#(\d+)\)$")
        for s in self.chat_sessions:
            s_title = s.title or s.display_title
            m = pattern.match(s_title)
            if m:
                try:
                    existing_nums.append(int(m.group(1)))
                except ValueError:
                    pass

        next_num = max(existing_nums, default=0) + 1
        fork_title = f"{base_title} (fork #{next_num})"

        # Create new session
        self._session_counter += 1
        new_sess = ChatSession(
            id=uuid.uuid4().hex[:8],
            num=self._session_counter,
            title=fork_title,
            has_prompt=source_sess.has_prompt if source_sess else False,
            tokens_in=source_sess.tokens_in if source_sess else 0,
            tokens_out=source_sess.tokens_out if source_sess else 0,
            mode=source_sess.mode if source_sess else self.current_mode,
            agent_id=getattr(source_sess, "agent_id", None) or self.active_agent_id,
        )
        self.chat_sessions.append(new_sess)

        # Clone UI messages
        source_msgs = self.chat_messages.get(source_session_id, [])
        self.chat_messages[new_sess.id] = [m.clone() for m in source_msgs]

        # Clone context memory in backend
        if hasattr(self.app.backend, "clone_session_memory"):
            try:
                self.app.backend.clone_session_memory(source_session_id, new_sess.id)
            except Exception as exc:
                logger.debug("Failed to clone backend session memory: %s", exc)

        return new_sess

    def _sync_agent_select(self) -> None:
        try:
            sel = self.query_one("#agent-select", Select)
            is_plan = (self.current_mode == "plan")
            sel.set_options(get_agent_options(is_plan_mode=is_plan))
            sel.value = self.active_agent_id
        except Exception:
            pass

    def _switch_to_session(self, session_id: str) -> None:
        self.active_session_id = session_id
        sess = self.active_chat_session
        if sess:
            self.current_mode = sess.mode
            if sess.agent_id:
                self.active_agent_id = sess.agent_id
            else:
                if self.current_mode == "plan":
                    self.active_agent_id = to_plan_agent(self.active_agent_id)
                else:
                    self.active_agent_id = to_build_agent(self.active_agent_id)
            self._sync_agent_select()
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        messages = self.chat_messages.get(session_id, [])
        if not messages:
            self._welcome()
        else:
            for msg in messages:
                w = msg.create_widget()
                chat.mount(w)
            self._scroll()
        self.refresh_session_list()
        self.update_bars()

    def refresh_session_list(self) -> None:
        try:
            lst = self.query_one("#session-list", VerticalScroll)
        except Exception:
            return
        lst.remove_children()
        rows = [
            SessionRow(s.id, s.display_title, s.id == self.active_session_id)
            for s in self.chat_sessions
        ]
        if rows:
            lst.mount(*rows)

    def delete_chat_session(self, session_id: str) -> None:
        idx = -1
        for i, s in enumerate(self.chat_sessions):
            if s.id == session_id:
                idx = i
                break
        self.chat_sessions = [s for s in self.chat_sessions if s.id != session_id]
        self.chat_messages.pop(session_id, None)
        try:
            if hasattr(self.app.backend, "_llm_engine"):
                self.app.backend._llm_engine.memory.sessions.pop(session_id, None)
        except Exception:
            pass

        if self.active_session_id == session_id:
            if self.chat_sessions:
                new_idx = min(idx, len(self.chat_sessions) - 1) if idx >= 0 else 0
                self._switch_to_session(self.chat_sessions[new_idx].id)
            else:
                new_sess = self.create_chat_session()
                self._switch_to_session(new_sess.id)
        else:
            self.refresh_session_list()

    def refresh_device_list(self) -> None:
        try:
            lst = self.query_one("#device-list", OptionList)
        except Exception:
            return
        lst.clear_options()
        sessions = getattr(self.app, "sessions", [])
        if not sessions:
            lst.add_option(Option(Text("  no connected devices", style="dim"), id=None, disabled=True))
            return
        for i, s in enumerate(sessions):
            is_active = (s.id == self.app.active_id)
            name = s.hostname or s.profile.name
            if len(name) > 18:
                name = name[:17] + "…"
            trans = s.profile.transport.value
            t = Text.assemble(
                (f"  {name}", "bold white" if is_active else "#999999"),
                (f"  {trans}", "bold #e4e4e7" if is_active else "dim #666666"),
            )
            lst.add_option(Option(t, id=s.id))
            if is_active:
                lst.highlighted = i

    def refresh_console_devices(self) -> None:
        try:
            sel = self.query_one("#console-device-select", Select)
        except Exception:
            return
        sessions = getattr(self.app, "sessions", [])
        if not sessions:
            if [v for _, v in sel._options if v not in (Select.BLANK, Select.NULL)] != ["none"]:
                sel.set_options([("No devices connected", "none")])
                sel.value = "none"
            self._update_console_header()
            return
        options = []
        for s in sessions:
            name = s.hostname or s.profile.name
            if len(name) > 30:
                name = name[:29] + "…"
            trans = s.profile.transport.value.upper()
            options.append((f"{name}  [{trans}]", s.id))
        cur_val = getattr(self.app, "active_id", None)
        existing = [(str(p.plain if hasattr(p, 'plain') else p), v) for p, v in sel._options if v not in (Select.BLANK, Select.NULL)]
        if existing != options:
            sel.set_options(options)
            if cur_val in [opt[1] for opt in options]:
                sel.value = cur_val
            elif options:
                sel.value = options[0][1]
        else:
            if cur_val in [opt[1] for opt in options] and sel.value != cur_val:
                sel.value = cur_val
        self._update_console_header()

    # ---- layout ----
    def compose(self) -> ComposeResult:
        with Horizontal(id="ws-body"):
            with Vertical(id="sidebar"):
                yield Static(build_sidebar_banner(), id="sidebar-banner")
                yield Label("devices", classes="section", id="sec-devices")
                yield OptionList(id="device-list")
                with Horizontal(classes="sidebar-btn-row"):
                    yield Button("+ connect device", id="add-device")
                yield Label("sessions", classes="section")
                with VerticalScroll(id="session-list"):
                    pass
                with Horizontal(classes="sidebar-btn-row"):
                    yield Button("+ new session", id="btn-new-session")
            yield SidebarDivider()
            with TabbedContent(id="main", initial="tab-agent"):
                with TabPane("agent", id="tab-agent"):
                    yield VerticalScroll(id="chat")
                    with Vertical(id="composer-container"):
                        yield CommandSuggestionsBox(id="composer-suggestions")
                        with Horizontal(id="composer-box"):
                            yield Static("❯", id="prompt-prefix")
                            yield PromptInput(placeholder="Ask anything, edit config, debug the network... (/ for commands)", id="prompt")
                        with Horizontal(id="composer-footer"):
                            yield Static("no devices connected  ·  0 / 200k tokens (0%)", id="composer-status")
                            with Horizontal(id="composer-actions"):
                                yield Select(get_agent_options(), value=self.active_agent_id, id="agent-select", allow_blank=False)
                                btn_model = Button("model", id="btn-model")
                                btn_model.can_focus = False
                                yield btn_model
                with TabPane("console", id="tab-console"):
                    with Horizontal(id="console-header"):
                        with Horizontal(id="console-hdr-left"):
                            yield Static("● CONNECTED", id="console-status-badge")
                            yield Select([], prompt="Select device...", id="console-device-select", allow_blank=True)
                        with Horizontal(id="console-hdr-mid"):
                            yield Static("", id="console-device-meta")
                        with Horizontal(id="console-hdr-right"):
                            yield Button("^C", id="cb-c", tooltip="Interrupt active command (Ctrl+C)")
                            yield Button("^Z", id="cb-z", tooltip="Exit to Exec prompt (Ctrl+Z)")
                            yield Button("break", id="cb-break", tooltip="Send Break sequence (Serial only)")
                            yield Button("↵ enter", id="cb-enter", tooltip="Send Carriage Return (<CR>)")
                            yield Button("clear", id="cb-clear", tooltip="Clear terminal output (Ctrl+L)")
                    yield RichLog(id="console", highlight=False, markup=False, wrap=True)
                    with Horizontal(id="console-input-box"):
                        yield Static("❯ ", id="console-prefix")
                        yield ConsoleInput(placeholder="Send raw CLI command to device... (Enter to send, ↑/↓ history)", id="console-input")
                with TabPane("settings", id="tab-settings"):
                    with VerticalScroll(id="tab-settings-scroll"):
                        with Vertical(id="settings-hdr"):
                            hdr = Text()
                            hdr.append("› ", style="bold #cccccc")
                            hdr.append("SETTINGS & PREFERENCES", style="bold white")
                            yield Static(hdr, id="settings-title")
                            yield Static("Configure safety guardrails and execution limits.", id="settings-subtitle")

                        # 1. Approval Policy & Safety
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("APPROVAL POLICY & SAFETY", classes="settings-card-title")
                                yield Static("Control human approval thresholds and secret sanitization.", classes="settings-card-desc")

                            with Vertical(classes="settings-item"):
                                yield Label("Approval Policy", classes="settings-field-lbl")
                                yield Select(
                                    [
                                        ("Config changes only (Recommended)", "ask_changes"),
                                        ("Autonomous (AI review & auto-approval)", "autonomous"),
                                        ("Dangerous actions only (AI danger check)", "auto_safe"),
                                        ("Every command", "ask_all"),
                                    ],
                                    value="ask_changes",
                                    allow_blank=False,
                                    id="ws-s-approval",
                                )
                                yield Static("Controls when the autonomous agent pauses for explicit confirmation.", classes="settings-field-help")

                            with Vertical(classes="settings-item"):
                                yield Label("Model Reasoning Effort / Thinking Variant", classes="settings-field-lbl")
                                yield Select(
                                    [
                                        ("Off / Minimal (Fastest, no thinking tokens)", "off"),
                                        ("Low (Light reasoning budget)", "low"),
                                        ("Medium (Balanced - Default)", "medium"),
                                        ("High (Deep reasoning budget)", "high"),
                                        ("Max / Extra High (Exhaustive analysis)", "max"),
                                    ],
                                    value="medium",
                                    allow_blank=False,
                                    id="ws-s-effort",
                                )
                                yield Static("Controls reasoning token budget and thinking depth for supported AI models.", classes="settings-field-help")

                            with Vertical(classes="settings-checkbox-item"):
                                yield Checkbox("Mask passwords, hashes, and SNMP community strings", True, id="ws-s-redact")
                                yield Static("Sanitizes sensitive credentials before console output reaches the LLM.", classes="settings-field-help")

                        # 2. Automated Pre-Change Snapshots
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("AUTOMATED PRE-CHANGE SNAPSHOTS", classes="settings-card-title")
                                yield Static("Capture configuration backups and health baselines before changes.", classes="settings-card-desc")

                            with Vertical(classes="settings-checkbox-item"):
                                yield Checkbox("Pull running-config before changes", True, id="ws-s-snap-cfg")
                                yield Static("Pulls full active configuration prior to applying changes.", classes="settings-field-help")

                            with Vertical(classes="settings-checkbox-item"):
                                yield Checkbox("Capture health statistics baseline (interfaces, routing, CPU)", False, id="ws-s-snap-stats")
                                yield Static("Records baseline telemetry to verify against after changes apply.", classes="settings-field-help")

                            with Vertical(classes="settings-item"):
                                yield Label("Snapshot Storage Destination", classes="settings-field-lbl")
                                yield Select(
                                    [
                                        ("Local machine (~/.rackhand/snapshots)", "local"),
                                        ("On-device storage (flash:)", "device"),
                                    ],
                                    value="local",
                                    allow_blank=False,
                                    id="ws-s-snap-dest",
                                )
                                yield Static("Choose whether config snapshots reside locally or on hardware flash.", classes="settings-field-help")

                        # 3. Terminal & Execution Limits
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("TERMINAL & EXECUTION LIMITS", classes="settings-card-title")
                                yield Static("CLI output pagination and autonomous execution boundaries.", classes="settings-card-desc")

                            with Vertical(classes="settings-checkbox-item"):
                                yield Checkbox("Disable CLI output paging automatically", True, id="ws-s-paging")
                                yield Static("Executes 'terminal length 0' or FortiOS equivalent on session connect.", classes="settings-field-help")

                            with Horizontal(classes="settings-row"):
                                with Vertical(classes="settings-col"):
                                    yield Label("Max steps per turn", classes="settings-field-lbl")
                                    yield Input("25", id="ws-s-steps", type="integer")
                                    yield Static("Turn loop limit before awaiting input.", classes="settings-field-help")
                                with Vertical(classes="settings-col"):
                                    yield Label("Command timeout (seconds)", classes="settings-field-lbl")
                                    yield Input("30", id="ws-s-timeout", type="integer")
                                    yield Static("Timeout before aborting stalled commands.", classes="settings-field-help")

                        # 4. Logging & Transcripts
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("LOGGING & TRANSCRIPTS", classes="settings-card-title")
                                yield Static("Audit trail retention and file paths.", classes="settings-card-desc")

                            with Vertical(classes="settings-checkbox-item"):
                                yield Checkbox("Save full session transcripts to disk", True, id="ws-s-transcripts")
                                yield Static("Persists raw device CLI input/output and agent reasoning.", classes="settings-field-help")

                            with Vertical(classes="settings-item"):
                                yield Label("Transcript Directory", classes="settings-field-lbl")
                                yield Input("~/.rackhand/transcripts", id="ws-s-dir")
                                yield Static("Path where session audit logs are stored.", classes="settings-field-help")

                        with Horizontal(classes="settings-btn-row"):
                            yield Button("Save settings", variant="primary", id="ws-s-save")
                            yield Button("Reset defaults", id="ws-s-reset")
                            yield Button("Return to Agent", id="ws-s-back")

    async def on_mount(self) -> None:
        for s in self.app.sessions:  # type: ignore[attr-defined]
            self._start_pump(s)
        if not self.chat_sessions:
            sess = self.create_chat_session()
            self.active_session_id = sess.id
        self.refresh_session_list()
        self.refresh_device_list()
        self.refresh_console_devices()
        self._render_console()
        self.update_bars()
        self._load_settings_tab()
        try:
            p_inp = self.query_one("#prompt", PromptInput)
            s_box = self.query_one("#composer-suggestions", CommandSuggestionsBox)
            p_inp.suggestions_box = s_box
            s_box.set_prompt_input(p_inp)
        except Exception:
            pass
        self._welcome()
        self.query_one("#prompt", Input).focus()

    # ---- chat primitives ----
    def _scroll(self) -> None:
        try:
            chat = self.query_one("#chat", VerticalScroll)
            self.call_after_refresh(chat.scroll_end, animate=False)
        except Exception:
            pass

    def _mount_message(self, msg: ChatMessage) -> Widget:
        w = msg.create_widget()
        try:
            self.query_one("#chat", VerticalScroll).mount(w)
            self._scroll()
        except Exception:
            pass
        if self.active_session_id:
            self.chat_messages.setdefault(self.active_session_id, []).append(msg)
        return w

    def _bubble(self, content: Any, classes: str = "") -> Widget:
        msg = GenericMessage(content, classes)
        return self._mount_message(msg)

    @staticmethod
    def _agent_panel(md: str) -> Panel:
        return Panel(ThemedMarkdown(md), title="rackhand", title_align="left", border_style="#2a2a2a",
                     box=box.ROUNDED, padding=(0, 1))

    @staticmethod
    def _tool_panel(call: AgentToolCall, res: Optional[AgentToolResult]) -> Panel:
        risk_color = {"read": "#555555", "config": "#888888", "danger": "#883333"}[call.risk.value]
        head = Text.assemble((call.device, "dim"), "  ", (call.risk.value, risk_color))
        body = Text.assemble(("$ ", "dim"), (call.command, "white"))
        if res is None:
            body.append("\n  executing...", style="dim")
        else:
            lines = res.output.splitlines()
            shown = "\n".join(lines[:14]) + (f"\n  ... {len(lines) - 14} more lines" if len(lines) > 14 else "")
            body.append("\n")
            body.append_text(highlight_config(shown))
        return Panel(body, title=head, title_align="left", border_style="#222222", box=box.ROUNDED,
                     padding=(0, 1))

    def _welcome(self) -> None:
        active_sess = self.active_chat_session
        sess_title = active_sess.display_title if active_sess else "Session #1"

        t_top = Text()
        t_top.append(f"{sess_title.upper()}\n", style="bold white")
        t_top.append("▲ Notice: ", style="bold #e4e4e7")
        t_top.append("Commands target live infrastructure. Make sure you know what your doing.", style="dim #a0a0a0")

        rule = Rule(style="#262626")

        t_bot = Text()
        t_bot.append("Ctrl+N", style="bold #cccccc")
        t_bot.append(" connect", style="dim #666666")
        t_bot.append("  ·  ", style="#333333")
        t_bot.append("F2", style="bold #cccccc")
        t_bot.append(" model", style="dim #666666")
        t_bot.append("  ·  ", style="#333333")
        t_bot.append("F3", style="bold #cccccc")
        t_bot.append(" settings", style="dim #666666")
        t_bot.append("  ·  ", style="#333333")
        t_bot.append("Ctrl+L", style="bold #cccccc")
        t_bot.append(" clear", style="dim #666666")

        self._bubble(Panel(Group(t_top, rule, t_bot), border_style="#222222", box=box.ROUNDED, padding=(0, 1)))

    @on(SessionRow.Selected)
    def _session_selected(self, ev: SessionRow.Selected) -> None:
        self._switch_to_session(ev.session_id)

    @on(SessionRow.Deleted)
    def _session_deleted(self, ev: SessionRow.Deleted) -> None:
        self.delete_chat_session(ev.session_id)

    @on(OptionList.OptionSelected, "#device-list")
    def _device_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_id:
            new_id = str(ev.option_id)
            if new_id != getattr(self.app, "active_id", None):
                self.app.active_id = new_id
                self._render_console()
                self.update_bars()
                self.refresh_device_list()
                s = self.app.active
                if s:
                    self.notify(f"Active: {s.hostname or s.profile.name}", title="Device")

    @on(Select.Changed, "#console-device-select")
    def _console_device_changed(self, ev: Select.Changed) -> None:
        if ev.value and ev.value != Select.BLANK and ev.value != "none":
            new_id = str(ev.value)
            if new_id != getattr(self.app, "active_id", None):
                self.app.active_id = new_id
                self._render_console()
                self.update_bars()
                self.refresh_device_list()
                s = self.app.active
                if s:
                    self.notify(f"Console active: {s.hostname or s.profile.name}", title="Device")

    def _update_console_header(self, busy: bool = False) -> None:
        try:
            badge = self.query_one("#console-status-badge", Static)
            meta = self.query_one("#console-device-meta", Static)
            prefix = self.query_one("#console-prefix", Static)
            inp = self.query_one("#console-input", ConsoleInput)
        except Exception:
            return

        s = self.app.active
        if not s:
            badge.update(Text("○ NO DEVICE", style="dim #666666"))
            badge.remove_class("-busy")
            badge.add_class("-offline")
            meta.update(Text("No active session · Connect a device to begin", style="dim #555555"))
            prefix.update(Text("❯ ", style="dim #555555"))
            inp.placeholder = "No device active (connect via sidebar or Ctrl+N)..."
            inp.disabled = True
            for bid in ("#cb-c", "#cb-z", "#cb-break", "#cb-enter", "#cb-clear"):
                try:
                    self.query_one(bid, Button).disabled = True
                except Exception:
                    pass
            return

        inp.disabled = False
        buf = self.buffers.get(s.id, [])
        line_count = len(buf)

        if busy:
            badge.update(Text("● RUNNING", style="bold #d4d4d8"))
            badge.add_class("-busy")
            badge.remove_class("-offline")
        else:
            badge.update(Text("● CONNECTED", style="bold #e4e4e7"))
            badge.remove_class("-busy")
            badge.remove_class("-offline")

        # Build technical device metadata
        t = Text()
        vendor_name = s.vendor.value.replace("_", " ").title() if hasattr(s.vendor, "value") else str(s.vendor)
        t.append(vendor_name, style="bold #e0e0e0")
        t.append("  ·  ", style="dim #444444")

        if s.profile.transport is Transport.SERIAL and s.profile.serial:
            t.append(f"{s.profile.serial.port} @ {s.profile.serial.baud or 115200}", style="#aaaaaa")
        elif s.profile.transport is Transport.SSH and s.profile.ssh:
            t.append(f"{s.profile.ssh.host}:{s.profile.ssh.port}", style="#aaaaaa")
        else:
            t.append(s.profile.transport.value.upper(), style="#aaaaaa")

        t.append("  ·  ", style="dim #444444")
        mode_label = "PRIVILEGED (#)" if "#" in s.prompt else "EXEC (>)" if ">" in s.prompt else s.mode.upper()
        t.append(mode_label, style="bold #cccccc")

        t.append("  ·  ", style="dim #444444")
        t.append(f"{line_count} line{'s' if line_count != 1 else ''}", style="dim #888888")

        meta.update(t)

        p_clean = s.prompt.strip()
        prefix.update(Text(f"{p_clean} " if p_clean else "❯ ", style="bold #e0e0e0"))
        inp.placeholder = f"Send CLI command to {s.hostname or s.profile.name}... (Enter to send, ↑/↓ history)"

        for bid in ("#cb-c", "#cb-z", "#cb-enter", "#cb-clear"):
            try:
                self.query_one(bid, Button).disabled = False
            except Exception:
                pass
        try:
            self.query_one("#cb-break", Button).disabled = not (s.profile.transport is Transport.SERIAL)
        except Exception:
            pass

    def _render_console(self) -> None:
        try:
            log = self.query_one("#console", RichLog)
        except Exception:
            return
        log.clear()
        sid = self.app.active_id  # type: ignore[attr-defined]
        buf = self.buffers.get(sid or "", [])
        if not sid or not buf:
            s = self.app.active
            if s:
                t = Text()
                t.append(f"  ── Terminal Session Active: {s.hostname or s.profile.name} ──\n", style="bold #e0e0e0")
                t.append(f"  Transport: {s.profile.transport.value.upper()}   Mode: {s.mode.upper()}\n\n", style="dim #777777")
                t.append(f"{s.prompt} ", style="bold white")
                log.write(t)
            else:
                t = Text()
                t.append("\n  No device connected to live console.\n", style="bold #888888")
                t.append("  Click '+ connect device' in the sidebar or use Ctrl+N to connect hardware.\n", style="dim #555555")
                log.write(t)
        else:
            for t in buf:
                log.write(t)
        self._update_console_header()
        try:
            log.scroll_end(animate=False)
        except Exception:
            pass

    def update_bars(self) -> None:
        app = self.app
        s = app.active  # type: ignore[attr-defined]
        prov = app.provider  # type: ignore[attr-defined]

        try:
            self.refresh_device_list()
            self.refresh_console_devices()
        except Exception:
            pass

        try:
            status_text = Text()
            if self.current_mode == "plan":
                status_text.append("[PLAN MODE]  ", style="bold #e4e4e7")
            count = len(self.app.sessions)
            if count > 0:
                status_text.append(f"{count} device{'s' if count != 1 else ''} in scope", style="#999999")
            else:
                status_text.append("no devices connected", style="#777777")

            # Context window usage (like Claude Code: e.g. 1.2k / 200k tokens (1%))
            max_tokens = get_model_context_window(prov)
            active_sess = self.active_chat_session
            used_tokens = 0
            if active_sess:
                used_tokens = active_sess.tokens_in + active_sess.tokens_out
            if used_tokens == 0:
                used_tokens = self.tokens_in + self.tokens_out
            if used_tokens == 0:
                msgs = self.chat_messages.get(self.active_session_id or "", [])
                chars = sum(len(m.markdown) if hasattr(m, "markdown") else len(m.text) if hasattr(m, "text") else 0 for m in msgs)
                if chars > 0:
                    used_tokens = chars // 4

            used_fmt = format_token_count(used_tokens)
            max_fmt = format_token_count(max_tokens)
            pct = (used_tokens / max_tokens) * 100 if max_tokens > 0 else 0
            if used_tokens == 0:
                pct_str = "0%"
            elif pct < 1:
                pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<1%"
            else:
                pct_str = f"{int(round(pct))}%"

            # Dynamic color coding: normal (<70%), warning (70-90%), critical (>90%)
            if pct >= 90:
                token_style = "bold #f87171"
            elif pct >= 70:
                token_style = "bold #d4d4d8"
            else:
                token_style = "dim #888888"

            status_text.append("  ·  ", style="dim #444444")
            status_text.append(f"{used_fmt} / {max_fmt} tokens ({pct_str})", style=token_style)

            self.query_one("#composer-status", Static).update(status_text)
            try:
                p_in = self.query_one("#prompt", Input)
                if self.current_mode == "plan":
                    p_in.placeholder = "Ask specialist agent (Plan Mode - read-only)... (/build to exit)"
                else:
                    p_in.placeholder = "Ask anything, edit config, debug the network... (/ for commands)"
            except Exception:
                pass
        except Exception:
            pass

        try:
            m_btn = self.query_one("#btn-model", Button)
            if prov and prov.model:
                clean_model = prov.model.split("/")[-1]
                m_btn.label = clean_model
            else:
                m_btn.label = "model"
        except Exception:
            pass

        try:
            self._update_console_header()
        except Exception:
            pass

    # ---- console ----
    def _start_pump(self, s: DeviceSession) -> None:
        self.buffers.setdefault(s.id, [])
        self.run_worker(self._pump(s), group=f"pump-{s.id}", exclusive=False)

    async def _pump(self, s: DeviceSession) -> None:
        async for chunk in self.app.backend.console_stream(s.id):  # type: ignore[attr-defined]
            t = fmt_bytes_prompt(chunk)
            if not t.plain and not chunk.text:
                continue
            self.buffers[s.id].append(t)

            # Auto-detect dynamic hostname from streaming console output
            detected = extract_hostname(chunk.text)
            if detected and detected != s.hostname:
                s.hostname = detected
                s.profile.name = detected
                self.update_bars()

            if s.id == self.app.active_id:  # type: ignore[attr-defined]
                try:
                    log = self.query_one("#console", RichLog)
                    log.write(t)
                except Exception:
                    pass
                self._update_console_header()

    @on(Input.Submitted, "#console-input")
    def _console_submit(self, ev: Input.Submitted) -> None:
        sid = self.app.active_id  # type: ignore[attr-defined]
        if not sid:
            self.notify("No active device connected.", severity="warning")
            return
        cmd = ev.value
        if hasattr(ev.input, "add_to_history"):
            ev.input.add_to_history(cmd)
        ev.input.value = ""
        self._send_console_command(sid, cmd)

    def _send_console_command(self, sid: str, cmd: str) -> None:
        self._update_console_header(busy=True)
        self.run_worker(self._run_console_cmd_worker(sid, cmd), group="console-cmd")

    async def _run_console_cmd_worker(self, sid: str, cmd: str) -> None:
        try:
            await self.app.backend.run_command(sid, cmd, "user")  # type: ignore[attr-defined]
        except Exception as e:
            self.notify(f"Command error: {e}", severity="error")
        finally:
            self._update_console_header(busy=False)
            try:
                log = self.query_one("#console", RichLog)
                log.scroll_end(animate=False)
            except Exception:
                pass

    @on(Button.Pressed, "#console-header Button")
    def _console_bar(self, ev: Button.Pressed) -> None:
        bid = ev.button.id or ""
        sid = self.app.active_id  # type: ignore[attr-defined]
        if bid == "cb-clear":
            self.action_clear_console()
            return
        if not sid:
            self.notify("No active device connected.", severity="warning")
            return
        action = {"cb-c": "ctrl_c", "cb-z": "ctrl_z", "cb-break": "break", "cb-enter": "cr"}.get(bid)
        if action:
            self._update_console_header(busy=True)
            self.run_worker(self._send_control_worker(sid, action), group="console-ctl")

    async def _send_control_worker(self, sid: str, action: str) -> None:
        try:
            await self.app.backend.send_control(sid, action)  # type: ignore[attr-defined]
        finally:
            self._update_console_header(busy=False)
            try:
                log = self.query_one("#console", RichLog)
                log.scroll_end(animate=False)
            except Exception:
                pass

    def action_clear_console(self) -> None:
        sid = self.app.active_id  # type: ignore[attr-defined]
        if sid:
            self.buffers[sid] = []
        try:
            log = self.query_one("#console", RichLog)
            log.clear()
            s = self.app.active
            if s:
                log.write(Text(f"{s.prompt} ", style="bold white"))
        except Exception:
            pass
        self._update_console_header()
        self.notify("Console buffer cleared.", title="Terminal")

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, ev: TabbedContent.TabActivated) -> None:
        if ev.pane.id == "tab-console":
            self.refresh_console_devices()
            self._update_console_header()
            try:
                self.query_one("#console-input", ConsoleInput).focus()
            except Exception:
                pass
            try:
                self.query_one("#console", RichLog).scroll_end(animate=False)
            except Exception:
                pass
        elif ev.pane.id == "tab-agent":
            try:
                self.query_one("#prompt", PromptInput).focus()
            except Exception:
                pass

    # ---- agent ----
    @on(Input.Submitted, "#prompt")
    def _prompt_submit(self, ev: Input.Submitted) -> None:
        self._send(ev.value)

    def _send(self, text: str) -> None:
        try:
            self.query_one("#composer-suggestions", CommandSuggestionsBox).hide_suggestions()
        except Exception:
            pass
        text = text.strip()
        app = self.app
        if not text or self._busy:
            return
        if text.startswith("/"):
            self.query_one("#prompt", Input).value = ""
            self._handle_command(text)
            return
        if app.active is None and self.current_mode != "plan":
            self.notify("Connect a device first (Ctrl+N).", severity="warning")
            return
        if app.provider is None:  # type: ignore[attr-defined]
            self.notify("Choose a model first (F2).", severity="warning")
            self.action_model()
            return
        self.query_one("#prompt", Input).value = ""
        user_msg = UserMessage(text)
        self._mount_message(user_msg)
        self._run_agent(text)

    @on(Select.Changed, "#agent-select")
    def _agent_select_changed(self, ev: Select.Changed) -> None:
        if ev.value and ev.value is not Select.BLANK and ev.value != Select.NULL:
            new_id = str(ev.value)
            if new_id != self.active_agent_id:
                self.active_agent_id = new_id
                if self.active_chat_session:
                    self.active_chat_session.agent_id = new_id
                agent = get_agent(new_id)
                if agent:
                    try:
                        self.notify(f"Active Agent: {agent.name}", title="Specialist Agent")
                    except Exception:
                        pass

    def _handle_command(self, text: str) -> None:
        parts = text[1:].strip().split(maxsplit=1)
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "plan":
            if self.current_mode == "plan":
                self._bubble(Panel(
                    Text("Plan mode is already enabled.\nType /build to exit plan mode.", style="bold #e4e4e7"),
                    border_style="#3f3f46",
                    box=box.ROUNDED,
                    title="Plan Mode Active",
                    padding=(0, 1),
                ))
                return

            self.current_mode = "plan"
            if self.active_chat_session:
                self.active_chat_session.mode = "plan"
            self.active_agent_id = to_plan_agent(self.active_agent_id)
            self._sync_agent_select()
            self.update_bars()

            plan_banner = (
                "### Plan Mode Activated\n\n"
                "Plan Mode is now active. All actions are strictly **read-only** (diagnostic commands only; configuration changes disabled).\n\n"
                "- Starting hardware discovery and CLI connectivity check...\n"
                "- Type `/build` at any time to exit Plan Mode and switch back to normal execution."
            )
            self._mount_message(AssistantMessage(markdown=plan_banner))

            # Kick off initial discovery turn
            if self.app.active is not None:
                self._run_agent("Identify connected devices, verify CLI read access, and ask the operator what task or setup needs to be planned.")
            else:
                self._mount_message(AssistantMessage(markdown="*No device is currently connected. You can discuss the plan with the specialist agent, or connect a device with `Ctrl+N` for live hardware discovery.*"))
            return

        if cmd == "build":
            if self.current_mode == "build":
                self._bubble(Panel(
                    Text("Already in Build mode (normal execution workflow).\nType /plan to enter plan mode.", style="bold #e4e4e7"),
                    border_style="#3f3f46",
                    box=box.ROUNDED,
                    title="Build Mode Active",
                    padding=(0, 1),
                ))
                return

            self.current_mode = "build"
            if self.active_chat_session:
                self.active_chat_session.mode = "build"
            self.active_agent_id = to_build_agent(self.active_agent_id)
            self._sync_agent_select()
            self.update_bars()

            build_banner = (
                "### Build Mode Activated\n\n"
                "Exited Plan Mode. Normal execution workflow is active.\n"
                "You may now execute commands, apply configuration changes, and proceed with implementation.\n\n"
                "- Type `/plan` at any time to re-enter Plan Mode."
            )
            self._mount_message(AssistantMessage(markdown=build_banner))
            return

        if cmd == "compact":
            self.action_compact()
            return

        if cmd == "fork":
            self.action_fork()
            return

        if cmd == "rename":
            self.action_rename(args)
            return

        if cmd in ("effort", "variant", "thinking"):
            self.action_effort(args)
            return

        if cmd in ("agent", "agents"):
            try:
                sel = self.query_one("#agent-select", Select)
                sel.focus()
                sel.action_show_overlay()
            except Exception:
                pass
            return

        if cmd in ("usage", "quota", "limits", "ratelimit"):
            self.action_show_usage()
            return

        if cmd in ("update", "upgrade", "version"):
            self.action_check_update()
            return

        if cmd in ("settings", "config", "preferences"):
            self.action_settings()
            return

        if cmd in ("help", "?"):
            help_text = Text()
            help_text.append("RACKHAND CLI COMMANDS\n\n", style="bold white")
            help_text.append("/plan", style="bold white")
            help_text.append("              Enter plan mode (discovery, read-only inspection, task planning)\n", style="dim")
            help_text.append("/build", style="bold white")
            help_text.append("             Exit plan mode to normal execution (apply changes & run tasks)\n", style="dim")
            help_text.append("/compact", style="bold white")
            help_text.append("           Summarize session checkpoint, clear history & continue\n", style="dim")
            help_text.append("/fork", style="bold white")
            help_text.append("              Clone conversation and open it in a new session\n", style="dim")
            help_text.append("/rename", style="bold white")
            help_text.append("            Rename the current session title (/rename <title>)\n", style="dim")
            help_text.append("/effort", style="bold white")
            help_text.append("            Choose model reasoning effort / variant (/variant, /thinking)\n", style="dim")
            help_text.append("/agent", style="bold white")
            help_text.append("             Expand specialist agent selector dropdown\n", style="dim")
            help_text.append("/usage", style="bold white")
            help_text.append("             Show provider usage, rate limits & token windows\n", style="dim")
            help_text.append("/update", style="bold white")
            help_text.append("            Check for GitHub updates and prompt for auto-update\n", style="dim")
            help_text.append("/settings", style="bold white")
            help_text.append("          Configure guardrails, execution limits & theme\n", style="dim")
            help_text.append("/clear", style="bold white")
            help_text.append("             Clear current session chat history\n", style="dim")
            help_text.append("/help", style="bold white")
            help_text.append("              Show this help menu", style="dim")
            self._bubble(Panel(help_text, border_style="#222222", box=box.ROUNDED, padding=(0, 1)))
            return

        if cmd == "clear":
            self.action_clear_chat()
            return

        self._bubble(Text(f"unknown command: /{cmd}. Type /help for available commands.", style="dim"))

    async def _open_agent_picker(self) -> None:
        picked = await self.app.push_screen_wait(AgentPickerModal(self.active_agent_id, is_plan_mode=(self.current_mode == "plan")))  # type: ignore[attr-defined]
        if picked:
            self.active_agent_id = picked
            if self.active_chat_session:
                self.active_chat_session.agent_id = picked
            try:
                self.query_one("#agent-select", Select).value = picked
            except Exception:
                pass
            found = get_agent(picked)
            if found:
                self._bubble(Text.assemble(
                    ("Active Specialist Agent: ", "white"),
                    (f"{found.badge} {found.name}\n", "bold #e4e4e7"),
                    (f"{found.description}", "dim")
                ))
                try:
                    self.notify(f"Active Agent: {found.name}", title="Specialist Agent")
                except Exception:
                    pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy

    def _needs_modal(self, c: ProposedChange) -> bool:
        mode = self.app.settings.approval_mode  # type: ignore[attr-defined]
        if c.risk is Risk.DANGER:
            return True
        if mode == "auto_safe":
            return False
        return not (c.risk is Risk.READ and mode == "ask_changes")

    @work(group="title_gen")
    async def _generate_session_title_ai(self, sess: ChatSession, prompt: str) -> None:
        try:
            provider = self.app.provider  # type: ignore[attr-defined]
            if not provider:
                return
            ai_title = await self.app.backend.generate_session_title(provider, prompt)  # type: ignore[attr-defined]
            if ai_title:
                sess.title = ai_title
                self.refresh_session_list()
                self.update_bars()
        except Exception:
            pass

    @work(exclusive=True, group="agent")
    async def _run_agent(self, prompt: str) -> None:
        app = self.app
        backend: Backend = app.backend  # type: ignore[attr-defined]
        self._set_busy(True)
        # Trigger AI session title generation on first prompt if not yet generated
        active_sess = self.active_chat_session
        if active_sess and not active_sess.has_prompt:
            active_sess.has_prompt = True
            self._generate_session_title_ai(active_sess, prompt)

        active_assistant_msg: Optional[AssistantMessage] = None
        active_tool_bundle: Optional[ToolBundleMessage] = None

        agent_spec = resolve_agent(self.active_agent_id, app.active)
        is_plan = (self.current_mode == "plan")

        try:
            async for ev in backend.run_turn(
                prompt,
                app.active,
                app.provider,
                app.settings,
                agent=agent_spec,
                chat_session_id=self.active_session_id,
                is_plan_mode=is_plan,
            ):
                if isinstance(ev, AgentText):
                    active_tool_bundle = None
                    if active_assistant_msg is None:
                        active_assistant_msg = AssistantMessage(markdown=ev.delta)
                        self._mount_message(active_assistant_msg)
                    else:
                        active_assistant_msg.markdown += ev.delta
                        active_assistant_msg.update_widget()
                        self._scroll()

                elif isinstance(ev, AgentToolCall):
                    active_assistant_msg = None
                    entry = ToolCallEntry(
                        call_id=ev.call_id,
                        device=ev.device,
                        command=ev.command,
                        risk=ev.risk,
                        running=True,
                    )
                    if active_tool_bundle is None:
                        active_tool_bundle = ToolBundleMessage(calls=[entry])
                        self._mount_message(active_tool_bundle)
                    else:
                        active_tool_bundle.add_call(entry)
                        active_tool_bundle.update_widget()
                        self._scroll()

                elif isinstance(ev, AgentToolResult):
                    if active_tool_bundle is not None and active_tool_bundle.set_result(ev.call_id, ev.output, ev.ok):
                        active_tool_bundle.update_widget()
                        self._scroll()
                    else:
                        sess_msgs = self.chat_messages.get(self.active_session_id or "", [])
                        for m in reversed(sess_msgs):
                            if isinstance(m, ToolBundleMessage) and m.set_result(ev.call_id, ev.output, ev.ok):
                                m.update_widget()
                                self._scroll()
                                break

                elif isinstance(ev, AgentProposal):
                    active_assistant_msg = None
                    active_tool_bundle = None
                    await self._handle_proposal(ev.change)

                elif isinstance(ev, AgentDone):
                    active_assistant_msg = None
                    active_tool_bundle = None
                    active_sess = self.active_chat_session
                    if active_sess:
                        active_sess.tokens_in += ev.tokens_in
                        active_sess.tokens_out += ev.tokens_out
                    self.tokens_in += ev.tokens_in
                    self.tokens_out += ev.tokens_out
                    self.update_bars()

                elif isinstance(ev, AgentError):
                    active_assistant_msg = None
                    active_tool_bundle = None
                    self._bubble(Panel(Text(ev.message, style="bold #f87171"), title="Error", border_style="#7f1d1d",
                                       title_align="left"))
        except asyncio.CancelledError:
            self._bubble(Text("■ execution aborted", style="dim"))
            raise
        except Exception as exc:
            self._bubble(Panel(Text(str(exc), style="bold #f87171"), title="Agent Error", border_style="#7f1d1d",
                               title_align="left"))
        finally:
            self._set_busy(False)

    async def _handle_proposal(self, c: ProposedChange) -> None:
        app = self.app
        self.pending[c.id] = c
        mode = app.settings.approval_mode  # type: ignore[attr-defined]

        # 1. Autonomous Policy: spawn fresh agent to review touched config and commands
        if mode == "autonomous":
            try:
                from reviewer import review_change_autonomous
                verdict = await review_change_autonomous(
                    change=c,
                    session=app.active,
                    provider=app.provider,
                    llm_engine=app.backend._llm_engine,
                    run_command_fn=app.backend.run_command,
                )
                if verdict.rollback:
                    c.rollback = verdict.rollback
                if verdict.risk == Risk.DANGER:
                    c.risk = Risk.DANGER

                if verdict.auto_approve:
                    decision = ApprovalDecision(
                        approved=True,
                        commands=list(c.commands),
                        save_after=app.settings.backup_before_change,
                        note=f"Autonomous Approval: {verdict.reason}",
                    )
                    self._mount_message(ProposalVerdictMessage(c, decision))
                    await app.backend.resolve_approval(c.id, decision)
                    return
                else:
                    if verdict.reason:
                        c.rationale = f"{c.rationale}\n\n› AI Reviewer: {verdict.reason}".strip()
                    decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))
            except Exception:
                decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))

        # 2. Dangerous actions only: spawn fresh agent to evaluate risk level
        elif mode == "auto_safe":
            try:
                from reviewer import review_danger_only
                verdict = await review_danger_only(
                    change=c,
                    session=app.active,
                    provider=app.provider,
                    llm_engine=app.backend._llm_engine,
                )
                if verdict.rollback:
                    c.rollback = verdict.rollback
                if verdict.risk == Risk.DANGER:
                    c.risk = Risk.DANGER

                if verdict.is_dangerous or c.risk is Risk.DANGER:
                    if verdict.reason:
                        c.rationale = f"{c.rationale}\n\n› Danger Gatekeeper: {verdict.reason}".strip()
                    decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))
                else:
                    decision = ApprovalDecision(
                        approved=True,
                        commands=list(c.commands),
                        save_after=False,
                        note=f"Safe: {verdict.reason}",
                    )
                    self._mount_message(ProposalVerdictMessage(c, decision))
                    await app.backend.resolve_approval(c.id, decision)
                    return
            except Exception:
                decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))

        # 3. Every command
        elif mode == "ask_all":
            decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))

        # 4. Config changes only (Default)
        else:
            if c.risk is not Risk.READ:
                decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))
            else:
                decision = ApprovalDecision(True, list(c.commands), False)

        self._mount_message(ProposalVerdictMessage(c, decision))
        await app.backend.resolve_approval(c.id, decision)

    # ---- actions ----
    def action_focus_prompt(self) -> None:
        self.query_one("#main", TabbedContent).active = "tab-agent"
        self.query_one("#prompt", Input).focus()

    def action_add_device(self) -> None:
        self.app.push_screen(ConnectScreen(), self._on_connected)

    def _on_connected(self, session: Optional[DeviceSession]) -> None:
        if session is None:
            return
        app = self.app
        if session not in app.sessions:  # type: ignore[attr-defined]
            app.sessions.append(session)  # type: ignore[attr-defined]
        app.active_id = session.id  # type: ignore[attr-defined]
        self._start_pump(session)
        self._render_console()
        self.update_bars()
        self._bubble(Text.assemble(
            ("device connected  ", "dim"),
            (session.hostname or session.profile.name, "bold white"),
        ))

    def action_model(self) -> None:
        prov = self.app.provider  # type: ignore[attr-defined]
        try:
            self.query_one("#prompt", Input).focus()
        except Exception:
            pass
        self.app.push_screen(ProvidersScreen(prov.key if prov else None), self._on_provider)

    def _on_provider(self, cfg: Optional[ProviderConfig]) -> None:
        if cfg:
            self.app.provider = cfg  # type: ignore[attr-defined]
            self.update_bars()
            self.notify(cfg.label, title="Model Set")
        try:
            self.query_one("#prompt", Input).focus()
        except Exception:
            pass

    def _load_settings_tab(self) -> None:
        try:
            s = self.app.settings
            self.query_one("#ws-s-approval", Select).value = s.approval_mode
            self.query_one("#ws-s-effort", Select).value = getattr(s, "reasoning_effort", "medium") or "medium"
            self.query_one("#ws-s-snap-cfg", Checkbox).value = s.snapshot_config
            self.query_one("#ws-s-snap-stats", Checkbox).value = s.snapshot_health_stats
            self.query_one("#ws-s-snap-dest", Select).value = s.snapshot_destination
            self.query_one("#ws-s-redact", Checkbox).value = s.redact_secrets
            self.query_one("#ws-s-paging", Checkbox).value = s.disable_paging
            self.query_one("#ws-s-transcripts", Checkbox).value = s.keep_transcripts
            self.query_one("#ws-s-dir", Input).value = s.transcript_dir
            self.query_one("#ws-s-steps", Input).value = str(s.max_agent_steps)
            self.query_one("#ws-s-timeout", Input).value = str(s.command_timeout_s)
        except Exception:
            pass

    def action_show_usage(self) -> None:
        def _post_usage_to_chat(snap: Any) -> None:
            t = Text()
            hdr_title = "SUBSCRIPTION USAGE & ROLLING WINDOWS" if snap.is_subscription else "API CREDITS & RATE LIMITS"
            t.append(f"{hdr_title}\n", style="bold white")
            t.append(f"Provider: {snap.provider_name}  ·  Model: {snap.model}\n", style="dim")
            t.append(f"Session Context: {snap.session_total_tokens:,} / {snap.context_window:,} tokens ({snap.context_percent:.1f}% used)\n\n", style="dim")

            elements: list[Any] = [t]

            if snap.is_subscription and snap.subscription_windows:
                table = Table(box=box.SIMPLE, show_header=True, header_style="bold #e4e4e7", border_style="#262626", padding=(0, 1))
                table.add_column("Quota Window", style="bold white")
                table.add_column("Window", justify="center", width=8)
                table.add_column("Remaining", justify="right", width=12)
                table.add_column("Consumed", justify="right", width=10)
                table.add_column("Cooldown Timer", justify="left")
                table.add_column("Refreshes At", justify="left")

                for w in snap.subscription_windows:
                    rem_style = "bold #e4e4e7" if w.remaining_percent >= 50 else ("bold #d4d4d8" if w.remaining_percent >= 20 else "bold #f87171")
                    cd_style = "bold #e4e4e7" if "ready" in w.cooldown_display.lower() else "bold #d4d4d8"
                    table.add_row(
                        w.name,
                        f"[{w.window.upper()}]" if w.window else "-",
                        Text(f"{w.remaining_percent:.1f}%", style=rem_style),
                        f"{w.used_percent:.1f}%",
                        Text(w.cooldown_display, style=cd_style),
                        w.reset_time or "-",
                    )
                elements.append(table)
            elif not snap.is_subscription:
                extra_lines = []
                if snap.api_quota.plan_name:
                    extra_lines.append(f"Plan: {snap.api_quota.plan_name}")
                if snap.api_quota.credits_remaining is not None:
                    extra_lines.append(f"Credits Remaining: ${snap.api_quota.credits_remaining:.2f}")
                if snap.rate_limits.has_data:
                    rl = snap.rate_limits
                    if rl.remaining_requests is not None and rl.limit_requests:
                        extra_lines.append(f"RPM: {rl.remaining_requests:,}/{rl.limit_requests:,}")
                    if rl.remaining_tokens is not None and rl.limit_tokens:
                        extra_lines.append(f"TPM: {rl.remaining_tokens:,}/{rl.limit_tokens:,}")
                if extra_lines:
                    elements.append(Text("  ·  ".join(extra_lines), style="bold #e4e4e7"))

            self._bubble(Panel(Group(*elements), title="usage", title_align="left", border_style="#222222", box=box.ROUNDED, padding=(0, 1)))

        tracker = getattr(self.app.backend, "usage_tracker", None)
        if not tracker:
            from usage_tracker import UsageTracker
            tracker = UsageTracker.get_instance()

        self.app.push_screen(
            UsageModal(
                tracker=tracker,
                provider=self.app.provider,
                session=self.active_chat_session,
                on_post_to_chat=_post_usage_to_chat,
            )
        )

    def action_settings(self) -> None:
        try:
            main_tabs = self.query_one("#main", TabbedContent)
            if main_tabs.active == "tab-settings":
                main_tabs.active = "tab-agent"
                self.query_one("#prompt", Input).focus()
            else:
                self._load_settings_tab()
                main_tabs.active = "tab-settings"
                try:
                    self.query_one("#ws-s-approval", Select).focus()
                except Exception:
                    pass
        except Exception:
            pass

    @work(exclusive=True, group="manual-update-check")
    async def action_check_update(self) -> None:
        from app import VERSION
        from ui.modals import UpdateModal
        from updater import check_for_update, fetch_latest_release
        self.notify("Checking GitHub for updates...", title="Rackhand Update", severity="information", timeout=2.0)
        try:
            rel = await check_for_update(VERSION)
            if rel:
                await self.app.push_screen(UpdateModal(current_version=VERSION, release=rel))
            else:
                latest = await fetch_latest_release()
                latest_title = f" ({latest.title})" if latest else ""
                self.notify(f"Rackhand is up to date ({VERSION}){latest_title}.", title="Rackhand Update", severity="information", timeout=4.0)
        except Exception as e:
            self.notify(f"Update check failed: {e}", title="Rackhand Update", severity="warning", timeout=4.0)

    @on(Button.Pressed, "#ws-s-reset")
    def _reset_settings_tab(self) -> None:
        default_s = Settings()
        try:
            self.query_one("#ws-s-approval", Select).value = default_s.approval_mode
            self.query_one("#ws-s-effort", Select).value = default_s.reasoning_effort
            self.query_one("#ws-s-snap-cfg", Checkbox).value = default_s.snapshot_config
            self.query_one("#ws-s-snap-stats", Checkbox).value = default_s.snapshot_health_stats
            self.query_one("#ws-s-snap-dest", Select).value = default_s.snapshot_destination
            self.query_one("#ws-s-redact", Checkbox).value = default_s.redact_secrets
            self.query_one("#ws-s-paging", Checkbox).value = default_s.disable_paging
            self.query_one("#ws-s-transcripts", Checkbox).value = default_s.keep_transcripts
            self.query_one("#ws-s-dir", Input).value = default_s.transcript_dir
            self.query_one("#ws-s-steps", Input).value = str(default_s.max_agent_steps)
            self.query_one("#ws-s-timeout", Input).value = str(default_s.command_timeout_s)
            self.notify("Settings reset to defaults. Click Save settings to apply.", title="Defaults")
        except Exception:
            pass

    @on(Button.Pressed, "#ws-s-back")
    def _back_to_agent(self) -> None:
        try:
            main_tabs = self.query_one("#main", TabbedContent)
            main_tabs.active = "tab-agent"
            self.query_one("#prompt", Input).focus()
        except Exception:
            pass

    @on(TabbedContent.TabActivated)
    def _on_tab_changed(self, ev: TabbedContent.TabActivated) -> None:
        if ev.pane and ev.pane.id == "tab-settings":
            self._load_settings_tab()

    @on(Button.Pressed, "#ws-s-save")
    def _save_settings_tab(self) -> None:
        q = self.query_one
        try:
            steps = int(q("#ws-s-steps", Input).value or 25)
        except ValueError:
            steps = 25
        try:
            timeout = int(q("#ws-s-timeout", Input).value or 30)
        except ValueError:
            timeout = 30

        snap_cfg = q("#ws-s-snap-cfg", Checkbox).value
        effort = sel_value(q("#ws-s-effort", Select)) or "medium"
        new_settings = Settings(
            approval_mode=sel_value(q("#ws-s-approval", Select)) or "ask_changes",
            redact_secrets=q("#ws-s-redact", Checkbox).value,
            backup_before_change=snap_cfg,
            snapshot_config=snap_cfg,
            snapshot_health_stats=q("#ws-s-snap-stats", Checkbox).value,
            snapshot_destination=sel_value(q("#ws-s-snap-dest", Select)) or "local",
            snapshot_dir=self.app.settings.snapshot_dir,
            keep_transcripts=q("#ws-s-transcripts", Checkbox).value,
            transcript_dir=q("#ws-s-dir", Input).value.strip() or self.app.settings.transcript_dir,
            max_agent_steps=steps,
            command_timeout_s=timeout,
            disable_paging=q("#ws-s-paging", Checkbox).value,
            theme=self.app.settings.theme,
            reasoning_effort=effort,
        )
        self.app.apply_settings(new_settings)
        self.notify("Settings saved", title="Configuration")

    @work(exclusive=True, group="disconnect")
    async def action_disconnect(self) -> None:
        app = self.app
        s = app.active  # type: ignore[attr-defined]
        if not s:
            return
        await app.backend.disconnect(s.id)  # type: ignore[attr-defined]
        self.workers.cancel_group(self, f"pump-{s.id}")
        app.sessions.remove(s)  # type: ignore[attr-defined]
        app.active_id = app.sessions[-1].id if app.sessions else None  # type: ignore[attr-defined]
        self.buffers.pop(s.id, None)
        self._render_console()
        self.update_bars()
        self._bubble(Text.assemble(("disconnected  ", "dim"), (s.hostname or s.profile.name, "dim")))

    @on(Button.Pressed, "#btn-new-session")
    def _on_new_session(self) -> None:
        sess = self.create_chat_session()
        self._switch_to_session(sess.id)
        self.query_one("#prompt", Input).focus()

    @on(Button.Pressed, "#add-device")
    def _add_pressed(self) -> None:
        self.action_add_device()

    @on(Button.Pressed, "#btn-model")
    def _btn_model(self) -> None:
        self.action_model()

    def action_compact(self) -> None:
        if self._busy:
            self.notify("Agent is currently busy. Please wait for execution to complete.", severity="warning")
            return
        prov = self.app.provider  # type: ignore[attr-defined]
        if not prov:
            self.notify("Choose a model first (F2).", severity="warning")
            self.action_model()
            return

        sess_id = self.active_session_id or ""
        ui_msgs = self.chat_messages.get(sess_id, [])
        mem_msgs = []
        if hasattr(self.app.backend, "get_session_messages"):
            try:
                mem_msgs = self.app.backend.get_session_messages(sess_id)
            except Exception:
                mem_msgs = []

        has_content = False
        if mem_msgs:
            has_content = any(m.get("role") != "system" for m in mem_msgs)
        if not has_content and ui_msgs:
            has_content = any(isinstance(m, (UserMessage, AssistantMessage)) for m in ui_msgs)

        if not has_content:
            self._bubble(Panel(
                Text("No conversation history to compact in this session.\nType a message or run a command first.", style="dim"),
                border_style="#333333",
                box=box.ROUNDED,
                padding=(0, 1),
            ))
            return

        self.notify("Compacting conversation history...", title="Compacting")
        self.run_worker(self._run_compact(sess_id), exclusive=True, group="compact")

    async def _run_compact(self, sess_id: str) -> None:
        self._set_busy(True)
        progress_msg = GenericMessage(
            Panel(
                Text("Compacting conversation history and generating session checkpoint...", style="italic #d4d4d8"),
                title="compacting",
                border_style="#3f3f46",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        self._mount_message(progress_msg)
        try:
            prov = self.app.provider  # type: ignore[attr-defined]
            active_device = self.app.active  # type: ignore[attr-defined]
            active_agent = get_agent(self.active_agent_id)
            is_plan = (self.current_mode == "plan")

            # Extract fallback transcript from UI messages if needed
            ui_transcript_parts = []
            for m in self.chat_messages.get(sess_id, []):
                if isinstance(m, UserMessage):
                    ui_transcript_parts.append(f"USER: {m.text}")
                elif isinstance(m, AssistantMessage):
                    if m.markdown.strip():
                        ui_transcript_parts.append(f"ASSISTANT: {m.markdown.strip()}")
                elif isinstance(m, CompactionCheckpointMessage):
                    ui_transcript_parts.append(f"PREVIOUS CHECKPOINT:\n{m.summary.strip()}")

            fallback_transcript = "\n\n".join(ui_transcript_parts)

            checkpoint_summary = await self.app.backend.compact_session(  # type: ignore[attr-defined]
                session_id=sess_id,
                provider=prov,
                session=active_device,
                agent=active_agent,
                is_plan_mode=is_plan,
                fallback_transcript=fallback_transcript,
            )

            # Clear UI chat messages
            self.chat_messages[sess_id] = []

            # Clear chat scroll container
            chat = self.query_one("#chat", VerticalScroll)
            chat.remove_children()

            # Mount the compaction checkpoint as the starting message
            checkpoint_widget = CompactionCheckpointMessage(summary=checkpoint_summary)
            self._mount_message(checkpoint_widget)

            # Reset token counters to reflect compacted context
            compaction_tokens = max(len(checkpoint_summary) // 4, 100)
            active_sess = self.active_chat_session
            if active_sess:
                active_sess.tokens_in = compaction_tokens
                active_sess.tokens_out = 0
            self.tokens_in = compaction_tokens
            self.tokens_out = 0
            self.update_bars()

            self.notify("Session context compacted successfully.", title="Compacted")
        except Exception as exc:
            logger.exception("Session compaction failed")
            try:
                if sess_id in self.chat_messages and progress_msg in self.chat_messages[sess_id]:
                    self.chat_messages[sess_id].remove(progress_msg)
                if progress_msg.widget:
                    progress_msg.widget.remove()
            except Exception:
                pass
            self.notify(f"Compaction failed: {exc}", severity="error")
            self._bubble(Panel(
                Text(f"Compaction failed: {exc}", style="bold #f87171"),
                title="Compaction Error",
                border_style="#7f1d1d",
                box=box.ROUNDED,
                padding=(0, 1),
            ))
        finally:
            self._set_busy(False)

    def action_fork(self) -> None:
        """Clone the active conversation into a new session and switch to it immediately."""
        if not self.active_session_id:
            self.notify("No active session to fork.", severity="warning")
            return

        new_sess = self.fork_chat_session(self.active_session_id)
        self._switch_to_session(new_sess.id)
        self.notify(f"Forked session: {new_sess.title}", title="Session Forked")
        try:
            self.query_one("#prompt", Input).focus()
        except Exception:
            pass

    def _sync_provider_model_variant(self, effort: str) -> Optional[str]:
        """If active model has an effort variant suffix (e.g. -low, -medium, -high), sync it with chosen effort."""
        prov = getattr(self.app, "provider", None)
        if not prov or not getattr(prov, "model", None):
            return None

        cur_model = prov.model.strip()
        pattern = re.compile(r'^(.*?)-(low|medium|high|xhigh|max)$', re.IGNORECASE)
        m = pattern.match(cur_model)
        if m:
            base_model = m.group(1)
            if effort in ("off", "none", "low"):
                target_suffix = "low"
            elif effort in ("high", "max", "xhigh"):
                target_suffix = "high"
            else:
                target_suffix = "medium"

            new_model = f"{base_model}-{target_suffix}"
            if new_model.lower() != cur_model.lower():
                prov.model = new_model
                if hasattr(self.app.backend, "save_provider_config"):
                    try:
                        self.run_worker(self.app.backend.save_provider_config(prov), group="save-prov")
                    except Exception:
                        pass
                self.update_bars()
                return new_model
        return None

    def action_effort(self, arg: str = "") -> None:
        """Set model reasoning effort / thinking variant (off, low, medium, high, max)."""
        raw = arg.strip().lower()
        valid_map = {
            "off": "off",
            "none": "off",
            "0": "off",
            "false": "off",
            "disabled": "off",
            "low": "low",
            "1": "low",
            "med": "medium",
            "medium": "medium",
            "default": "medium",
            "2": "medium",
            "high": "high",
            "deep": "high",
            "3": "high",
            "max": "max",
            "xhigh": "max",
            "extra-high": "max",
            "4": "max",
        }
        if raw:
            if raw in valid_map:
                normalized = valid_map[raw]
                self.app.settings.reasoning_effort = normalized
                self.app.apply_settings(self.app.settings)
                model_swapped = self._sync_provider_model_variant(normalized)
                extra = f"\nActive Model variant switched to: {model_swapped}" if model_swapped else ""
                self.notify(f"Reasoning effort set to: {normalized.upper()}", title="Model Effort")
                self._bubble(Panel(
                    Text(f"Model reasoning effort updated to: {normalized.upper()}{extra}\nChoice has been saved and will persist across sessions.", style="bold #e4e4e7"),
                    border_style="#3f3f46",
                    box=box.ROUNDED,
                    title="Model Effort",
                    padding=(0, 1),
                ))
            else:
                self._bubble(Panel(
                    Text(f"Unknown effort level '{arg}'.\nValid options: off, low, medium, high, max\nUsage: /effort <level>", style="bold #e4e4e7"),
                    border_style="#52525b",
                    box=box.ROUNDED,
                    title="Invalid Effort",
                    padding=(0, 1),
                ))
            return

        cur = getattr(self.app.settings, "reasoning_effort", "medium") or "medium"
        self._bubble(Panel(
            Text(f"Current reasoning effort: {cur.upper()}\nUsage: /effort <level>\nValid options: off, low, medium, high, max", style="bold #e4e4e7"),
            border_style="#3f3f46",
            box=box.ROUNDED,
            title="Model Effort",
            padding=(0, 1),
        ))

    def action_rename(self, new_title: str = "") -> None:
        """Rename the current session title."""
        title = new_title.strip()
        if not title:
            self._bubble(Panel(
                Text("Usage: /rename <new title>\nRenames the current active chat session.", style="bold #e4e4e7"),
                title="Rename Session Title",
                border_style="#52525b",
                box=box.ROUNDED,
                padding=(0, 1),
            ))
            return

        sess = self.active_chat_session
        if not sess:
            self.notify("No active session to rename.", severity="warning")
            return

        sess.title = title
        sess.has_prompt = True  # Prevent automatic AI titler from overwriting the user's custom title
        self.refresh_session_list()
        self.update_bars()
        self.notify(f"Session renamed to: {title}", title="Session Renamed")
        self._bubble(Panel(
            Text(f"Session renamed to: {title}", style="bold #e4e4e7"),
            title="Session Renamed",
            border_style="#3f3f46",
            box=box.ROUNDED,
            padding=(0, 1),
        ))

    def action_clear_chat(self) -> None:
        try:
            main_tabs = self.query_one("#main", TabbedContent)
            if main_tabs.active == "tab-console":
                self.action_clear_console()
                return
        except Exception:
            pass
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        if self.active_session_id:
            self.chat_messages[self.active_session_id] = []
            if hasattr(self.app.backend, "clear_session_memory"):
                try:
                    self.app.backend.clear_session_memory(self.active_session_id)
                except Exception:
                    pass
        self._welcome()
