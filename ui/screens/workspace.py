from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from rich import box
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
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
    AssistantMessage, ChatMessage, GenericMessage, ProposalVerdictMessage,
    ToolBundleMessage, ToolCallEntry, UserMessage,
)
from ui.suggestions import CommandSuggestionsBox, PromptInput
from agents import AGENTS, get_agent, get_agent_options, resolve_agent
from ui.modals import (
    AgentPickerModal, ApprovalModal, ConnectScreen, ProvidersScreen, SettingsModal,
)
from ui.styles import (
    MenuStatic, SelectableStatic, build_sidebar_banner, fmt_bytes_prompt,
    generate_session_title, highlight_config, pill, sel_value,
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
        dot = "* " if self.is_active else "  "
        title = self.title_text
        if len(title) > 17:
            title = title[:16] + "…"
        t = Text.assemble(
            (dot, "bold #22c55e" if self.is_active else "dim"),
            (title, "bold white" if self.is_active else "dim"),
        )
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
        )
        self.chat_sessions.append(sess)
        self.chat_messages[sess.id] = []
        return sess

    def _switch_to_session(self, session_id: str) -> None:
        self.active_session_id = session_id
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
            dot = "*" if s.id == self.app.active_id else " "
            name = s.hostname or s.profile.name
            if len(name) > 18:
                name = name[:17] + "…"
            trans = s.profile.transport.value
            t = Text.assemble(
                (f"{dot} ", "bold white" if dot == "*" else "dim"),
                (name, "bold white" if dot == "*" else "dim"),
                (f" ({trans})", "dim"),
            )
            lst.add_option(Option(t, id=s.id))
            if s.id == self.app.active_id:
                lst.highlighted = i

    def refresh_console_devices(self) -> None:
        try:
            sel = self.query_one("#console-device-select", Select)
        except Exception:
            return
        sessions = getattr(self.app, "sessions", [])
        if not sessions:
            if [v for _, v in sel._options if v not in (Select.BLANK, Select.NULL)] != ["none"]:
                sel.set_options([("no devices connected", "none")])
                sel.value = "none"
            self._update_console_header()
            return
        options = []
        for s in sessions:
            name = s.hostname or s.profile.name
            if len(name) > 18:
                name = name[:17] + "…"
            options.append((f"{name} ({s.profile.transport.value})", s.id))
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
                yield Label("devices", classes="section")
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
                                btn_settings = Button("settings", id="btn-composer-settings")
                                btn_settings.can_focus = False
                                yield btn_settings
                with TabPane("console", id="tab-console"):
                    with Horizontal(id="console-header"):
                        with Horizontal(id="console-hdr-left"):
                            yield Static("● CONNECTED", id="console-status-badge")
                            yield Select([], prompt="no devices", id="console-device-select", allow_blank=True)
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

                        # 1. Safety & Guardrails
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("SAFETY & GUARDRAILS", classes="settings-card-title")
                                yield Static("Control human approval thresholds and secret sanitization.", classes="settings-card-desc")

                            yield Label("Approval Policy", classes="settings-field-lbl")
                            yield Select(
                                [
                                    ("Config changes only (Recommended)", "ask_changes"),
                                    ("Every command", "ask_all"),
                                    ("Dangerous actions only", "auto_safe"),
                                ],
                                value="ask_changes",
                                allow_blank=False,
                                id="ws-s-approval",
                            )
                            yield Static("Controls when the autonomous agent pauses for explicit confirmation.", classes="settings-field-help")

                            yield Checkbox("Snapshot running-config before applying changes", True, id="ws-s-backup")
                            yield Static("Archives active config to NVRAM rollback history prior to changes.", classes="settings-field-help")

                            yield Checkbox("Mask passwords, hashes, and SNMP community strings", True, id="ws-s-redact")
                            yield Static("Sanitizes sensitive credentials before console output reaches the LLM.", classes="settings-field-help")

                        # 2. Terminal & Execution Limits
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("TERMINAL & EXECUTION LIMITS", classes="settings-card-title")
                                yield Static("CLI output pagination and autonomous execution boundaries.", classes="settings-card-desc")

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

                        # 3. Logging & Transcripts
                        with Vertical(classes="settings-card"):
                            with Vertical(classes="settings-card-header"):
                                yield Static("LOGGING & TRANSCRIPTS", classes="settings-card-title")
                                yield Static("Audit trail retention and file paths.", classes="settings-card-desc")

                            yield Checkbox("Save full session transcripts to disk", True, id="ws-s-transcripts")
                            yield Static("Persists raw device CLI input/output and agent reasoning.", classes="settings-field-help")

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
        return Panel(Markdown(md), title="rackhand", title_align="left", border_style="#2a2a2a",
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
        sess_title = active_sess.display_title if active_sess else "Session 1#"

        t_top = Text()
        t_top.append(f"{sess_title.upper()}\n", style="bold white")
        t_top.append("▲ Notice: ", style="bold #f59e0b")
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
            badge.update(Text("● RUNNING", style="bold #f59e0b"))
            badge.add_class("-busy")
            badge.remove_class("-offline")
        else:
            badge.update(Text("● CONNECTED", style="bold #10b981"))
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
                token_style = "bold #ef4444"
            elif pct >= 70:
                token_style = "bold #f59e0b"
            else:
                token_style = "dim #888888"

            status_text.append("  ·  ", style="dim #444444")
            status_text.append(f"{used_fmt} / {max_fmt} tokens ({pct_str})", style=token_style)

            self.query_one("#composer-status", Static).update(status_text)
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
        if app.active is None:  # type: ignore[attr-defined]
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

        if cmd in ("agent", "agents"):
            if not args:
                self.run_worker(self._open_agent_picker(), group="agent_picker")
                return
            if args.lower() in ("list", "all", "help", "?"):
                lines = [Text("AVAILABLE SPECIALIST AGENTS\n", style="bold white")]
                for ag in AGENTS:
                    is_act = ag.id == self.active_agent_id
                    prefix = "● " if is_act else "  "
                    style = "bold #38bdf8" if is_act else "bold white"
                    lines.append(Text.assemble(
                        (prefix, "bold #38bdf8" if is_act else "dim"),
                        (f"{ag.badge} ", "bold #f59e0b" if ag.id == "auto" else "bold cyan"),
                        (f"{ag.name} ", style),
                        (f"(/agent {ag.id})\n", "dim italic"),
                        (f"   {ag.description}\n", "dim"),
                    ))
                self._bubble(Panel(Group(*lines), border_style="#222222", box=box.ROUNDED, padding=(0, 1)))
                return

            found = get_agent(args)
            if found:
                self.active_agent_id = found.id
                try:
                    self.query_one("#agent-select", Select).value = found.id
                except Exception:
                    pass
                self._bubble(Text.assemble(
                    ("Active Specialist Agent: ", "white"),
                    (f"{found.badge} {found.name}\n", "bold #38bdf8"),
                    (f"{found.description}", "dim")
                ))
                try:
                    self.notify(f"Active Agent: {found.name}", title="Specialist Agent")
                except Exception:
                    pass
            else:
                self._bubble(Text(f"Unknown agent '{args}'. Type /agent list to see all available specialist agents.", style="red"))
            return

        if cmd in ("settings", "config", "preferences"):
            self.action_settings()
            return

        if cmd in ("help", "?"):
            help_text = Text()
            help_text.append("RACKHAND CLI COMMANDS\n\n", style="bold white")
            help_text.append("/agent", style="bold #38bdf8")
            help_text.append("             Open interactive specialist agent picker\n", style="dim")
            help_text.append("/agent <name>", style="bold #38bdf8")
            help_text.append("      Switch directly (e.g. /agent cisco-asa, /agent fortigate)\n", style="dim")
            help_text.append("/agent list", style="bold #38bdf8")
            help_text.append("        List all specialist agents and descriptions\n", style="dim")
            help_text.append("/settings", style="bold #38bdf8")
            help_text.append("          Configure guardrails, execution limits & theme\n", style="dim")
            help_text.append("/clear", style="bold #38bdf8")
            help_text.append("             Clear current session chat history\n", style="dim")
            help_text.append("/help", style="bold #38bdf8")
            help_text.append("              Show this help menu", style="dim")
            self._bubble(Panel(help_text, border_style="#222222", box=box.ROUNDED, padding=(0, 1)))
            return

        if cmd == "clear":
            self.action_clear_chat()
            return

        self._bubble(Text(f"unknown command: /{cmd}. Type /help for available commands.", style="dim"))

    async def _open_agent_picker(self) -> None:
        picked = await self.app.push_screen_wait(AgentPickerModal(self.active_agent_id))  # type: ignore[attr-defined]
        if picked:
            self.active_agent_id = picked
            try:
                self.query_one("#agent-select", Select).value = picked
            except Exception:
                pass
            found = get_agent(picked)
            if found:
                self._bubble(Text.assemble(
                    ("Active Specialist Agent: ", "white"),
                    (f"{found.badge} {found.name}\n", "bold #38bdf8"),
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

    @work(exclusive=True, group="agent")
    async def _run_agent(self, prompt: str) -> None:
        app = self.app
        backend: Backend = app.backend  # type: ignore[attr-defined]
        self._set_busy(True)
        # Auto-generate session title on first prompt if not yet generated
        active_sess = self.active_chat_session
        if active_sess and not active_sess.has_prompt:
            active_sess.has_prompt = True
            active_sess.title = generate_session_title(prompt, app.active)
            self.refresh_session_list()
            self.update_bars()

        active_assistant_msg: Optional[AssistantMessage] = None
        active_tool_bundle: Optional[ToolBundleMessage] = None

        agent_spec = resolve_agent(self.active_agent_id, app.active)

        try:
            async for ev in backend.run_turn(
                prompt,
                app.active,
                app.provider,
                app.settings,
                agent=agent_spec,
                chat_session_id=self.active_session_id,
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
                    self._bubble(Panel(Text(ev.message, style="bold red"), title="Error", border_style="red",
                                       title_align="left"))
        except asyncio.CancelledError:
            self._bubble(Text("■ execution aborted", style="dim"))
            raise
        except Exception as exc:
            self._bubble(Panel(Text(str(exc), style="bold red"), title="Agent Error", border_style="red",
                               title_align="left"))
        finally:
            self._set_busy(False)

    async def _handle_proposal(self, c: ProposedChange) -> None:
        app = self.app
        self.pending[c.id] = c
        if self._needs_modal(c):
            decision = await app.push_screen_wait(ApprovalModal(c, app.settings.backup_before_change))  # type: ignore[attr-defined]
        else:
            decision = ApprovalDecision(True, list(c.commands), False)
        self._mount_message(ProposalVerdictMessage(c, decision))
        await app.backend.resolve_approval(c.id, decision)  # type: ignore[attr-defined]

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
            self.query_one("#ws-s-redact", Checkbox).value = s.redact_secrets
            self.query_one("#ws-s-backup", Checkbox).value = s.backup_before_change
            self.query_one("#ws-s-paging", Checkbox).value = s.disable_paging
            self.query_one("#ws-s-transcripts", Checkbox).value = s.keep_transcripts
            self.query_one("#ws-s-dir", Input).value = s.transcript_dir
            self.query_one("#ws-s-steps", Input).value = str(s.max_agent_steps)
            self.query_one("#ws-s-timeout", Input).value = str(s.command_timeout_s)
        except Exception:
            pass

    def action_settings(self) -> None:
        try:
            main_tabs = self.query_one("#main", TabbedContent)
            if main_tabs.active == "tab-settings":
                main_tabs.active = "tab-agent"
                self.query_one("#prompt", Input).focus()
            else:
                self._load_settings_tab()
                main_tabs.active = "tab-settings"
        except Exception:
            pass

    @on(Button.Pressed, "#btn-composer-settings")
    def _open_settings(self) -> None:
        self.action_settings()

    @on(Button.Pressed, "#ws-s-reset")
    def _reset_settings_tab(self) -> None:
        default_s = Settings()
        try:
            self.query_one("#ws-s-approval", Select).value = default_s.approval_mode
            self.query_one("#ws-s-redact", Checkbox).value = default_s.redact_secrets
            self.query_one("#ws-s-backup", Checkbox).value = default_s.backup_before_change
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

        new_settings = Settings(
            approval_mode=sel_value(q("#ws-s-approval", Select)) or "ask_changes",
            redact_secrets=q("#ws-s-redact", Checkbox).value,
            backup_before_change=q("#ws-s-backup", Checkbox).value,
            keep_transcripts=q("#ws-s-transcripts", Checkbox).value,
            transcript_dir=q("#ws-s-dir", Input).value.strip() or self.app.settings.transcript_dir,
            max_agent_steps=steps,
            command_timeout_s=timeout,
            disable_paging=q("#ws-s-paging", Checkbox).value,
            theme=self.app.settings.theme,
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
        self._welcome()
