from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from models import Settings
from ui.styles import sel_value


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
            with Vertical(id="settings-hdr"):
                hdr = Text()
                hdr.append("› ", style="bold #cccccc")
                hdr.append("SETTINGS & PREFERENCES", style="bold white")
                yield Static(hdr, id="settings-title")
                yield Static("Configure safety guardrails, execution limits, and workspace appearance.", id="settings-subtitle")

            with VerticalScroll(id="settings-scroll"):
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
                        value=s.approval_mode,
                        allow_blank=False,
                        id="s-approval",
                    )
                    yield Static("Controls when the autonomous agent pauses for explicit confirmation.", classes="settings-field-help")

                    yield Checkbox("Snapshot running-config before applying changes", s.backup_before_change, id="s-backup")
                    yield Static("Archives active config to NVRAM rollback history prior to changes.", classes="settings-field-help")

                    yield Checkbox("Mask passwords, hashes, and SNMP community strings", s.redact_secrets, id="s-redact")
                    yield Static("Sanitizes sensitive credentials before console output reaches the LLM.", classes="settings-field-help")

                # 2. Terminal & Execution Limits
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("TERMINAL & EXECUTION LIMITS", classes="settings-card-title")
                        yield Static("CLI output pagination and autonomous execution boundaries.", classes="settings-card-desc")

                    yield Checkbox("Disable CLI output paging automatically", s.disable_paging, id="s-paging")
                    yield Static("Executes 'terminal length 0' or FortiOS equivalent on session connect.", classes="settings-field-help")

                    with Horizontal(classes="settings-row"):
                        with Vertical(classes="settings-col"):
                            yield Label("Max steps per turn", classes="settings-field-lbl")
                            yield Input(str(s.max_agent_steps), id="s-steps", type="integer")
                            yield Static("Turn loop limit before awaiting input.", classes="settings-field-help")
                        with Vertical(classes="settings-col"):
                            yield Label("Command timeout (seconds)", classes="settings-field-lbl")
                            yield Input(str(s.command_timeout_s), id="s-timeout", type="integer")
                            yield Static("Timeout before aborting stalled commands.", classes="settings-field-help")

                # 3. Logging & Transcripts
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("LOGGING & TRANSCRIPTS", classes="settings-card-title")
                        yield Static("Audit trail retention and file paths.", classes="settings-card-desc")

                    yield Checkbox("Save full session transcripts to disk", s.keep_transcripts, id="s-transcripts")
                    yield Static("Persists raw device CLI input/output and agent reasoning.", classes="settings-field-help")

                    yield Label("Transcript Directory", classes="settings-field-lbl")
                    yield Input(s.transcript_dir, id="s-dir")
                    yield Static("Path where session audit logs are stored.", classes="settings-field-help")

                # 4. Appearance
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("APPEARANCE", classes="settings-card-title")
                        yield Static("Color scheme for workspace and terminal cards.", classes="settings-card-desc")

                    yield Label("Color Theme", classes="settings-field-lbl")
                    yield Select(
                        [(t, t) for t in THEMES],
                        value=s.theme if s.theme in THEMES else "rackhand",
                        allow_blank=False,
                        id="s-theme",
                    )
                    yield Static("Terminal theme applied across all screens and widgets.", classes="settings-field-help")

            with Horizontal(classes="settings-btn-row"):
                yield Button("Save settings", variant="primary", id="s-save")
                yield Button("Cancel", id="s-cancel")

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
            theme=sel_value(q("#s-theme", Select)) or "rackhand",
        ))
