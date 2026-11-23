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
        with Vertical(id="settings", classes="dialog"):
            with Vertical(id="settings-hdr"):
                hdr = Text()
                hdr.append("› ", style="bold #cccccc")
                hdr.append("SETTINGS & PREFERENCES", style="bold white")
                yield Static(hdr, id="settings-title")
                yield Static("Configure safety guardrails and execution limits.", id="settings-subtitle")

            with VerticalScroll(id="settings-scroll"):
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
                            value=s.approval_mode,
                            allow_blank=False,
                            id="s-approval",
                        )
                        yield Static("Controls when the autonomous agent pauses for explicit confirmation.", classes="settings-field-help")

                    with Vertical(classes="settings-checkbox-item"):
                        yield Checkbox("Mask passwords, hashes, and SNMP community strings", s.redact_secrets, id="s-redact")
                        yield Static("Sanitizes sensitive credentials before console output reaches the LLM.", classes="settings-field-help")

                # 2. Automated Pre-Change Snapshots
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("AUTOMATED PRE-CHANGE SNAPSHOTS", classes="settings-card-title")
                        yield Static("Capture configuration backups and health baselines before changes.", classes="settings-card-desc")

                    with Vertical(classes="settings-checkbox-item"):
                        yield Checkbox("Pull running-config before changes", s.snapshot_config, id="s-snap-cfg")
                        yield Static("Pulls full active configuration prior to applying changes.", classes="settings-field-help")

                    with Vertical(classes="settings-checkbox-item"):
                        yield Checkbox("Capture health statistics baseline (interfaces, routing, CPU)", s.snapshot_health_stats, id="s-snap-stats")
                        yield Static("Records baseline telemetry to verify against after changes apply.", classes="settings-field-help")

                    with Vertical(classes="settings-item"):
                        yield Label("Snapshot Storage Destination", classes="settings-field-lbl")
                        yield Select(
                            [
                                ("Local machine (~/.rackhand/snapshots)", "local"),
                                ("On-device storage (flash:)", "device"),
                            ],
                            value=s.snapshot_destination,
                            allow_blank=False,
                            id="s-snap-dest",
                        )
                        yield Static("Choose whether config snapshots reside locally or on hardware flash.", classes="settings-field-help")

                # 3. Terminal & Execution Limits
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("TERMINAL & EXECUTION LIMITS", classes="settings-card-title")
                        yield Static("CLI output pagination and autonomous execution boundaries.", classes="settings-card-desc")

                    with Vertical(classes="settings-checkbox-item"):
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

                # 4. Logging & Transcripts
                with Vertical(classes="settings-card"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("LOGGING & TRANSCRIPTS", classes="settings-card-title")
                        yield Static("Audit trail retention and file paths.", classes="settings-card-desc")

                    with Vertical(classes="settings-checkbox-item"):
                        yield Checkbox("Save full session transcripts to disk", s.keep_transcripts, id="s-transcripts")
                        yield Static("Persists raw device CLI input/output and agent reasoning.", classes="settings-field-help")

                    with Vertical(classes="settings-item"):
                        yield Label("Transcript Directory", classes="settings-field-lbl")
                        yield Input(s.transcript_dir, id="s-dir")
                        yield Static("Path where session audit logs are stored.", classes="settings-field-help")

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
        snap_cfg = q("#s-snap-cfg", Checkbox).value
        self.dismiss(Settings(
            approval_mode=sel_value(q("#s-approval", Select)) or "ask_changes",
            redact_secrets=q("#s-redact", Checkbox).value,
            backup_before_change=snap_cfg,
            snapshot_config=snap_cfg,
            snapshot_health_stats=q("#s-snap-stats", Checkbox).value,
            snapshot_destination=sel_value(q("#s-snap-dest", Select)) or "local",
            snapshot_dir=self.s.snapshot_dir,
            keep_transcripts=q("#s-transcripts", Checkbox).value,
            transcript_dir=q("#s-dir", Input).value.strip() or self.s.transcript_dir,
            max_agent_steps=_int(q("#s-steps", Input).value, 25),
            command_timeout_s=_int(q("#s-timeout", Input).value, 30),
            disable_paging=q("#s-paging", Checkbox).value,
            theme=self.s.theme,
        ))
