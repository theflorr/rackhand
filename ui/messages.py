from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from rich import box
from rich.console import Group
from rich.markdown import BlockQuote, Heading, Markdown
from rich.panel import Panel
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Button, Static

from models import ApprovalDecision, ProposedChange, Risk
from ui.styles import SelectableStatic, highlight_config, pill


# =============================================================================
# CHAT MESSAGE ARCHITECTURE
# =============================================================================

class ChatMessage(ABC):
    def __init__(self, css_class: str = ""):
        self.css_class = css_class
        self.widget: Optional[Widget] = None
        self.created_at: datetime = datetime.now()

    @abstractmethod
    def render(self) -> Any:
        ...

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional[ChatMessage]:
        return deserialize_message(data)

    @property
    def full_classes(self) -> str:
        return f"bubble {self.css_class}".strip()

    def create_widget(self) -> Widget:
        from ui.styles import SelectableStatic
        w = SelectableStatic(self.render(), classes=self.full_classes)
        self.widget = w
        return w

    def update_widget(self) -> None:
        if self.widget is not None:
            try:
                if hasattr(self.widget, "refresh_content"):
                    self.widget.refresh_content()
                elif hasattr(self.widget, "update"):
                    self.widget.update(self.render())
            except Exception:
                pass

    def clone(self) -> ChatMessage:
        import copy
        new_msg = copy.copy(self)
        new_msg.widget = None
        return new_msg


class GenericMessage(ChatMessage):
    def __init__(self, content: Any, css_class: str = ""):
        super().__init__(css_class)
        self.content = content

    def render(self) -> Any:
        return self.content

    def clone(self) -> GenericMessage:
        msg = GenericMessage(self.content, self.css_class)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        plain = self.content.plain if hasattr(self.content, "plain") else str(self.content)
        return {
            "type": "generic",
            "content": plain,
            "css_class": self.css_class,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GenericMessage:
        msg = cls(data.get("content", ""), css_class=data.get("css_class", ""))
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg


class UserMessage(ChatMessage):
    def __init__(
        self,
        text: str,
        attachments: Optional[dict[str, Any]] = None,
        status: Optional[str] = None,
    ):
        super().__init__(css_class="user")
        self.text = text
        self.attachments: dict[str, Any] = attachments or {}
        self.status: Optional[str] = status  # None, "sending", "sent"
        self.spinner_frame: str = "⠋"

    def render(self) -> Text:
        t = Text()
        t.append("you", style="bold white")
        t.append(f"  {self.created_at.strftime('%H:%M')}", style="dim")
        if self.status == "sending":
            t.append(f"  {self.spinner_frame} Sending to agent...", style="bold #38bdf8")
        elif self.status == "sent":
            t.append("  ✓ Sent to agent", style="dim #22c55e")
        t.append("\n")
        # Render text with badge highlights for [IMAGE #N] and [N characters/lines]
        parts = re.split(r"(\[IMAGE #\d+\]|\[\d+ (?:characters|chars|lines)\])", self.text)
        for part in parts:
            if not part:
                continue
            if re.match(r"^\[IMAGE #\d+\]$", part):
                t.append(part, style="bold #38bdf8 on #0f2744")
            elif re.match(r"^\[\d+ (?:characters|chars|lines)\]$", part):
                t.append(part, style="bold #cbd5e1 on #27272a")
            else:
                t.append(part, style="white")
        return t

    def clone(self) -> UserMessage:
        msg = UserMessage(self.text, attachments=dict(self.attachments), status=self.status)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        d = {
            "type": "user",
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }
        if self.attachments:
            d["attachments"] = self.attachments
        if self.status:
            d["status"] = self.status
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserMessage:
        msg = cls(data.get("text", ""), attachments=data.get("attachments"), status=data.get("status"))
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg


class ThemeHeading(Heading):
    """Custom Heading renderer that applies theme-matching colors instead of Rich default magenta."""
    STYLE_MAP = {
        "h1": "bold #ffffff",
        "h2": "bold #38bdf8",
        "h3": "bold #e4e4e7",
        "h4": "bold #a1a1aa",
        "h5": "bold #71717a",
        "h6": "dim #71717a",
    }

    def on_enter(self, context: Any) -> None:
        self.text = Text()
        style = self.STYLE_MAP.get(self.tag, "bold #e4e4e7")
        context.enter_style(style)


class ThemeBlockQuote(BlockQuote):
    """Custom BlockQuote renderer using subtle slate instead of Rich default magenta."""
    style_name = "italic #a1a1aa"


class ThemedMarkdown(Markdown):
    """Markdown component pre-configured with Rackhand theme styles."""
    elements = dict(Markdown.elements)
    elements["heading_open"] = ThemeHeading
    elements["blockquote_open"] = ThemeBlockQuote


class AssistantMessage(ChatMessage):
    def __init__(self, markdown: str = ""):
        super().__init__(css_class="assistant")
        self.markdown = markdown

    def render(self) -> Panel:
        body = ThemedMarkdown(self.markdown) if self.markdown.strip() else Text("● thinking...", style="dim")
        return Panel(
            body,
            title="rackhand",
            title_align="left",
            border_style="#262626",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def clone(self) -> AssistantMessage:
        msg = AssistantMessage(self.markdown)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "assistant",
            "markdown": self.markdown,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AssistantMessage:
        msg = cls(data.get("markdown", ""))
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg


class CompactionCheckpointMessage(ChatMessage):
    def __init__(self, summary: str = "", tokens_compacted: int = 0):
        super().__init__(css_class="assistant")
        self.summary = summary
        self.tokens_compacted = tokens_compacted

    def render(self) -> Panel:
        body = ThemedMarkdown(self.summary) if self.summary.strip() else Text("No checkpoint summary generated.", style="dim")
        sub_text = "Session Checkpoint · Prior turns compacted"
        return Panel(
            body,
            title="checkpoint: session compacted",
            subtitle=sub_text,
            title_align="left",
            subtitle_align="right",
            border_style="#3f3f46",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def clone(self) -> CompactionCheckpointMessage:
        msg = CompactionCheckpointMessage(self.summary, self.tokens_compacted)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "checkpoint",
            "summary": self.summary,
            "tokens_compacted": self.tokens_compacted,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionCheckpointMessage:
        msg = cls(
            summary=data.get("summary", ""),
            tokens_compacted=int(data.get("tokens_compacted", 0)),
        )
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg


@dataclass
class ToolCallEntry:
    call_id: str
    device: str
    command: str
    risk: Risk = Risk.READ
    output: Optional[str] = None
    ok: bool = True
    running: bool = True


class ToolBundleWidget(Vertical):
    DEFAULT_CSS = """
    ToolBundleWidget {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        background: #111111;
        border: round #262626;
    }
    ToolBundleWidget:hover {
        border: round #383838;
    }
    .tool-header-row {
        width: 100%;
        height: 1;
        layout: horizontal;
        align-vertical: middle;
    }
    .tool-title-static {
        width: 1fr;
        height: 1;
        color: #888888;
    }
    .btn-tool-toggle {
        width: auto;
        min-width: 11;
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: #1e1e1e;
        color: #999999;
        border: none;
    }
    .btn-tool-toggle:hover {
        background: #2a2a2a;
        color: #ffffff;
    }
    .btn-tool-toggle:focus {
        background: #333333;
        color: #ffffff;
    }
    .tool-compact-body {
        width: 100%;
        height: auto;
        padding: 0 0 0 1;
    }
    .tool-detail-body {
        width: 100%;
        height: auto;
        display: none;
        margin-top: 1;
        padding-top: 1;
        border-top: solid #222222;
    }
    ToolBundleWidget.-expanded .tool-detail-body {
        display: block;
    }
    """

    def __init__(self, msg: ToolBundleMessage):
        super().__init__()
        self.msg = msg
        self.expanded = getattr(msg, "expanded", False)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="tool-header-row", id="tool-hdr"):
            yield Static(self._render_title(), id="tool-title-lbl", classes="tool-title-static")
            yield Button("▾ Collapse" if self.expanded else "▸ Expand", id="btn-tool-expand", classes="btn-tool-toggle")
        yield SelectableStatic(self._render_compact(), id="tool-compact-lbl", classes="tool-compact-body")
        yield SelectableStatic(self._render_detail(), id="tool-detail-lbl", classes="tool-detail-body")

    def on_mount(self) -> None:
        if self.expanded:
            self.add_class("-expanded")
        else:
            self.remove_class("-expanded")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-tool-expand":
            event.stop()
            self.toggle()

    def on_click(self, event: Click) -> None:
        if event.widget and event.widget.id == "tool-compact-lbl":
            event.stop()
            self.toggle()

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.msg.expanded = self.expanded
        try:
            btn = self.query_one("#btn-tool-expand", Button)
            if self.expanded:
                self.add_class("-expanded")
                btn.label = "▾ Collapse"
            else:
                self.remove_class("-expanded")
                btn.label = "▸ Expand"
        except Exception:
            pass

    def refresh_content(self) -> None:
        try:
            self.query_one("#tool-title-lbl", Static).update(self._render_title())
            self.query_one("#tool-compact-lbl", SelectableStatic).update(self._render_compact())
            self.query_one("#tool-detail-lbl", SelectableStatic).update(self._render_detail())
        except Exception:
            pass

    def _render_title(self) -> Text:
        running_count = sum(1 for c in self.msg.calls if c.running)
        t = Text()
        t.append("› ", style="bold #71717a")
        if len(self.msg.calls) == 1:
            t.append(f"operation · {self.msg.calls[0].device}", style="bold #cccccc")
        else:
            t.append(f"operations · {len(self.msg.calls)} commands", style="bold #cccccc")
        if running_count > 0:
            t.append(f" ({running_count} running)", style="bold #d4d4d8")
        else:
            all_ok = all(c.ok for c in self.msg.calls)
            t.append(" ✓ completed", style="dim #a1a1aa" if all_ok else "bold #f87171")
        return t

    def _render_compact(self) -> Text:
        t = Text()
        first_row = True
        for c in self.msg.calls:
            # Handle agents sending commands containing \r\n, \r, or \n
            raw_cmd = (c.command or "").replace("\r\n", "\n").replace("\r", "\n")
            cmd_lines = [ln.strip() for ln in raw_cmd.split("\n") if ln.strip()]
            if not cmd_lines:
                # If command was only whitespace or pure carriage return / newline
                cmd_lines = ["<CR>"] if raw_cmd else ["<empty>"]

            for j, cmd_text in enumerate(cmd_lines):
                if not first_row:
                    t.append("\n")
                first_row = False

                if c.running:
                    t.append("● ", style="bold #d4d4d8")
                elif c.ok:
                    t.append("✓ ", style="#a1a1aa")
                else:
                    t.append("✗ ", style="bold #f87171")
                t.append(f"$ {cmd_text}", style="white")
                t.append(f" ({c.device})", style="dim")
                if not c.running and j == len(cmd_lines) - 1:
                    lines = len(c.output.splitlines()) if c.output else 0
                    t.append(f" [{lines} line{'s' if lines != 1 else ''}]", style="dim")
        return t

    def _render_detail(self) -> Any:
        elements = []
        for i, c in enumerate(self.msg.calls):
            if i > 0:
                elements.append(Text("─" * 52, style="#222222"))

            raw_cmd = (c.command or "").replace("\r\n", "\n").replace("\r", "\n")
            cmd_lines = [ln.strip() for ln in raw_cmd.split("\n") if ln.strip()]
            if not cmd_lines:
                cmd_lines = ["<CR>"] if raw_cmd else ["<empty>"]

            hdr = Text()
            for j, cmd_text in enumerate(cmd_lines):
                if j > 0:
                    hdr.append("\n")
                hdr.append("$ ", style="dim")
                hdr.append(cmd_text, style="bold white")
                if j == 0:
                    hdr.append(f"  ({c.device})", style="dim")
                    hdr.append(f"  [{c.risk.value}]", style="#555555")
                    if c.running:
                        hdr.append("  ● executing...", style="bold #d4d4d8")
                    else:
                        lines = len(c.output.splitlines()) if c.output else 0
                        status_text = f"✓ done ({lines} lines)" if c.ok else "✗ failed"
                        hdr.append(f"  {status_text}", style="#a1a1aa" if c.ok else "bold #f87171")
            elements.append(hdr)

            if c.output is not None:
                norm_out = c.output.replace("\r\n", "\n").replace("\r", "\n")
                lines = norm_out.splitlines()
                if not lines:
                    elements.append(Text("  (no output)\n", style="dim"))
                else:
                    shown_lines = lines[:40]
                    if len(lines) > 40:
                        shown_lines.append(f"... ({len(lines) - 40} more lines)")
                    shown = "\n".join("  " + l for l in shown_lines)
                    body = highlight_config(shown)
                    elements.append(body)
        return Group(*elements) if elements else Text("no commands", style="dim")


class ToolBundleMessage(ChatMessage):
    def __init__(self, calls: Optional[list[ToolCallEntry]] = None, expanded: bool = False):
        super().__init__(css_class="tool-bundle")
        self.calls: list[ToolCallEntry] = calls or []
        self.expanded: bool = expanded

    def add_call(self, call_entry: ToolCallEntry) -> None:
        self.calls.append(call_entry)

    def set_result(self, call_id: str, output: str, ok: bool = True) -> bool:
        for c in self.calls:
            if c.call_id == call_id:
                c.output = output
                c.ok = ok
                c.running = False
                return True
        return False

    def clone(self) -> ToolBundleMessage:
        import copy
        new_calls = [copy.copy(c) for c in self.calls]
        msg = ToolBundleMessage(calls=new_calls, expanded=self.expanded)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        calls_data = []
        for c in self.calls:
            calls_data.append({
                "call_id": c.call_id,
                "device": c.device,
                "command": c.command,
                "risk": c.risk.value if hasattr(c.risk, "value") else str(c.risk),
                "output": c.output,
                "ok": c.ok,
                "running": False,
            })
        return {
            "type": "tool_bundle",
            "expanded": self.expanded,
            "created_at": self.created_at.isoformat(),
            "calls": calls_data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolBundleMessage:
        calls = []
        for cd in data.get("calls", []):
            try:
                risk_val = Risk(cd.get("risk", "read"))
            except Exception:
                risk_val = Risk.READ
            calls.append(ToolCallEntry(
                call_id=str(cd.get("call_id", "")),
                device=str(cd.get("device", "")),
                command=str(cd.get("command", "")),
                risk=risk_val,
                output=cd.get("output"),
                ok=bool(cd.get("ok", True)),
                running=bool(cd.get("running", False)),
            ))
        msg = cls(calls=calls, expanded=bool(data.get("expanded", False)))
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg

    def create_widget(self) -> Widget:
        w = ToolBundleWidget(self)
        self.widget = w
        return w

    def render(self) -> Panel:
        elements = []
        running_count = sum(1 for c in self.calls if c.running)

        for i, c in enumerate(self.calls):
            if i > 0:
                elements.append(Text("─" * 48, style="#222222"))

            raw_cmd = (c.command or "").replace("\r\n", "\n").replace("\r", "\n")
            cmd_lines = [ln.strip() for ln in raw_cmd.split("\n") if ln.strip()]
            if not cmd_lines:
                cmd_lines = ["<CR>"] if raw_cmd else ["<empty>"]

            header = Text()
            for j, cmd_text in enumerate(cmd_lines):
                if j > 0:
                    header.append("\n")
                header.append("$ ", style="dim")
                header.append(cmd_text, style="bold white")
                if j == 0:
                    header.append(f"  ({c.device})", style="dim")
                    header.append(f"  [{c.risk.value}]", style="#555555")
                    if c.running:
                        header.append("  ● executing...", style="white")
                    else:
                        line_count = len(c.output.splitlines()) if c.output is not None else 0
                        status_text = f"✓ done ({line_count} line{'s' if line_count != 1 else ''})" if c.ok else "✗ failed"
                        header.append(f"  {status_text}", style="dim" if c.ok else "bold #f87171")

            elements.append(header)

            if c.output is not None:
                norm_out = c.output.replace("\r\n", "\n").replace("\r", "\n")
                lines = norm_out.splitlines()
                if not lines:
                    elements.append(Text("  (no output)\n", style="dim"))
                else:
                    shown_lines = lines[:12]
                    if len(lines) > 12:
                        shown_lines.append(f"... ({len(lines) - 12} more lines)")
                    shown = "\n".join("  " + l for l in shown_lines)
                    body = highlight_config(shown)
                    elements.append(body)

        if len(self.calls) == 1:
            title = f"operation · {self.calls[0].device}"
        else:
            title = f"operations · {len(self.calls)} commands"
        if running_count > 0:
            title += f" ({running_count} running)"

        return Panel(
            Group(*elements) if elements else Text("no commands", style="dim"),
            title=title,
            title_align="left",
            border_style="#262626",
            box=box.ROUNDED,
            padding=(0, 1),
        )


class ProposalVerdictMessage(ChatMessage):
    def __init__(self, change: ProposedChange, decision: ApprovalDecision):
        super().__init__(css_class="proposal")
        self.change = change
        self.decision = decision

    def render(self) -> Text:
        if self.decision.approved and self.decision.note and "Autonomous" in self.decision.note:
            verdict = pill("AUTONOMOUS", "ok")
        elif self.decision.approved:
            verdict = pill("APPROVED", "ok")
        else:
            verdict = pill("REJECTED", "err")
        edited = "  (edited)" if self.decision.approved and self.decision.commands != self.change.commands else ""
        res = Text.assemble(verdict, f"  {self.change.title}", (edited, "dim"))
        if self.decision.note:
            res.append(f"\n  › {self.decision.note}", style="dim #888888")
        return res

    def clone(self) -> ProposalVerdictMessage:
        msg = ProposalVerdictMessage(self.change, self.decision)
        msg.created_at = self.created_at
        return msg

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "proposal_verdict",
            "created_at": self.created_at.isoformat(),
            "change": {
                "id": self.change.id,
                "device": self.change.device,
                "title": self.change.title,
                "commands": list(self.change.commands),
                "risk": self.change.risk.value if hasattr(self.change.risk, "value") else str(self.change.risk),
                "rationale": self.change.rationale,
                "rollback": list(self.change.rollback),
                "before": self.change.before,
                "after": self.change.after,
            },
            "decision": {
                "approved": self.decision.approved,
                "commands": list(self.decision.commands),
                "save_after": self.decision.save_after,
                "note": self.decision.note,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProposalVerdictMessage:
        cd = data.get("change", {})
        dd = data.get("decision", {})
        try:
            risk_val = Risk(cd.get("risk", "read"))
        except Exception:
            risk_val = Risk.READ
        change = ProposedChange(
            id=str(cd.get("id", "")),
            device=str(cd.get("device", "")),
            title=str(cd.get("title", "")),
            commands=list(cd.get("commands", [])),
            risk=risk_val,
            rationale=str(cd.get("rationale", "")),
            rollback=list(cd.get("rollback", [])),
            before=str(cd.get("before", "")),
            after=str(cd.get("after", "")),
        )
        decision = ApprovalDecision(
            approved=bool(dd.get("approved", False)),
            commands=list(dd.get("commands", [])),
            save_after=bool(dd.get("save_after", False)),
            note=str(dd.get("note", "")),
        )
        msg = cls(change, decision)
        if "created_at" in data:
            try:
                msg.created_at = datetime.fromisoformat(data["created_at"])
            except Exception:
                pass
        return msg


def serialize_message(msg: ChatMessage) -> dict[str, Any]:
    if hasattr(msg, "to_dict"):
        return msg.to_dict()
    return {
        "type": "generic",
        "content": str(msg.render() if hasattr(msg, "render") else ""),
        "css_class": getattr(msg, "css_class", ""),
        "created_at": getattr(msg, "created_at", datetime.now()).isoformat(),
    }


def deserialize_message(data: dict[str, Any]) -> Optional[ChatMessage]:
    if not isinstance(data, dict):
        return None
    m_type = data.get("type", "")
    if m_type == "user":
        return UserMessage.from_dict(data)
    elif m_type == "assistant":
        return AssistantMessage.from_dict(data)
    elif m_type == "checkpoint":
        return CompactionCheckpointMessage.from_dict(data)
    elif m_type == "tool_bundle":
        return ToolBundleMessage.from_dict(data)
    elif m_type == "proposal_verdict":
        return ProposalVerdictMessage.from_dict(data)
    elif m_type == "generic":
        return GenericMessage.from_dict(data)
    return None
