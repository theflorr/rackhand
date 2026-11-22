from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from models import APPROVAL_LABELS, Settings
from ui.styles import MenuStatic, sel_value


def _int(s: str, default: int) -> int:
    try:
        return int(s.strip())
    except Exception:
        return default

THEMES = ["rackhand", "tokyo-night", "nord", "gruvbox", "dracula", "catppuccin-mocha", "textual-dark"]


class SettingsModal(ModalScreen[Optional[Settings]]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.s = settings

    def compose(self) -> ComposeResult:
        s = self.s
        with Vertical(id="settings", classes="dialog small"):
            yield MenuStatic(Text("SETTINGS", style="bold white"), classes="title")
            with VerticalScroll(id="settings-scroll"):
                yield Label("Approval gate")
                yield Select([("Every command", "ask_all"),
                              ("Config changes only", "ask_changes"),
                              ("Dangerous actions only", "auto_safe")],
                             value=s.approval_mode, allow_blank=False, id="s-approval")
                yield Checkbox("Redact passwords & secrets", s.redact_secrets, id="s-redact")
                yield Checkbox("Backup config before changes", s.backup_before_change, id="s-backup")
                yield Checkbox("Disable terminal paging", s.disable_paging, id="s-paging")
                yield Checkbox("Save transcripts", s.keep_transcripts, id="s-transcripts")
                yield Label("Transcript directory")
                yield Input(s.transcript_dir, id="s-dir")
                with Horizontal(classes="row"):
                    with Vertical(classes="col"):
                        yield Label("Max steps / turn")
                        yield Input(str(s.max_agent_steps), id="s-steps", type="integer")
                    with Vertical(classes="col"):
                        yield Label("Timeout (s)")
                        yield Input(str(s.command_timeout_s), id="s-timeout", type="integer")
                yield Label("Theme")
                yield Select([(t, t) for t in THEMES], value=s.theme if s.theme in THEMES else "rackhand",
                             allow_blank=False, id="s-theme")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="s-cancel")
                yield Button("Save", variant="primary", id="s-save")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#s-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#s-save")
    def _save(self) -> None:
        q = self.query_one
        self.dismiss(Settings(
            approval_mode=sel_value(q("#s-approval", Select)) or "ask_changes",
            redact_secrets=q("#s-redact", Checkbox).value,
            backup_before_change=q("#s-backup", Checkbox).value,
            keep_transcripts=q("#s-transcripts", Checkbox).value,
            transcript_dir=q("#s-dir", Input).value.strip() or self.s.transcript_dir,
            max_agent_steps=_int(q("#s-steps", Input).value, 25),
            command_timeout_s=_int(q("#s-timeout", Input).value, 30),
            disable_paging=q("#s-paging", Checkbox).value,
            theme=sel_value(q("#s-theme", Select)) or "rackhand"))


