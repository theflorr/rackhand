"""Encrypted Vault Setup, Unlock, and Passphrase Management Modals."""

from __future__ import annotations

import asyncio
from typing import Optional

import zxcvbn
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from vault import (
    DecryptionError,
    InvalidPassphraseError,
    MigrationVerificationError,
    TamperedDataError,
    get_vault,
)


def safe_zxcvbn(password: str) -> dict:
    """Safely compute zxcvbn score without raising IndexError on empty or invalid input."""
    if not password:
        return {"score": 0, "feedback": {"warning": ""}}
    try:
        return zxcvbn.zxcvbn(password)
    except Exception:
        return {"score": 0, "feedback": {"warning": ""}}


class VaultSetupModal(ModalScreen[bool]):
    """Modal dialog for initializing master passphrase and encrypting local data."""

    DEFAULT_CSS = """
    VaultSetupModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #vault-setup-dialog {
        width: 68;
        height: auto;
        border: round #27272a;
        background: #111115;
        padding: 1 2;
    }
    #vault-setup-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    #vault-setup-desc {
        color: #a1a1aa;
        margin-bottom: 1;
    }
    .field-label {
        color: #94a3b8;
        margin-top: 1;
        margin-bottom: 0;
    }
    #setup-passphrase, #setup-confirm {
        background: #09090b;
        border: round #27272a;
        color: #ffffff;
        margin-bottom: 0;
    }
    #setup-passphrase:focus, #setup-confirm:focus {
        border: round #38bdf8;
    }
    #setup-strength-container {
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
    }
    #setup-strength-bar {
        color: #e2e8f0;
    }
    #setup-strength-hint {
        color: #94a3b8;
    }
    #setup-checkbox-container {
        height: auto;
        width: 100%;
        margin-top: 1;
        margin-bottom: 1;
    }
    #setup-checkbox {
        width: 3;
        min-width: 3;
        height: auto;
        padding: 0;
        margin: 0;
        border: none;
        background: transparent;
    }
    #setup-checkbox-label {
        width: 1fr;
        height: auto;
        color: #d4d4d8;
        padding-left: 1;
    }
    #setup-status {
        color: #38bdf8;
        margin-top: 1;
        display: none;
    }
    .setup-buttons {
        width: 100%;
        align: right middle;
        height: auto;
        margin-top: 1;
    }
    #setup-submit-btn {
        min-width: 16;
    }
    """

    BINDINGS = [
        Binding("escape", "no_cancel", "Cannot Cancel", show=False),
    ]

    def __init__(self, migration_mode: bool = False) -> None:
        super().__init__()
        self.migration_mode = migration_mode

    def action_no_cancel(self) -> None:
        self.notify("A master passphrase is required to secure your data.", severity="warning")

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-setup-dialog"):
            yield Label("Create vault", id="vault-setup-title")
            desc_text = (
                "Rackhand encrypts all settings, credentials, API keys, sessions, "
                "and backups in a single secure local vault."
            )
            if self.migration_mode:
                desc_text += " Existing unencrypted local data will be securely migrated and verified."
            yield Static(desc_text, id="vault-setup-desc")

            yield Label("Master Passphrase", classes="field-label")
            yield Input(placeholder="Enter master passphrase", password=True, id="setup-passphrase")

            yield Label("Confirm Master Passphrase", classes="field-label")
            yield Input(placeholder="Confirm master passphrase", password=True, id="setup-confirm")

            with Vertical(id="setup-strength-container"):
                yield Static(self._render_strength_bar(0, 0), id="setup-strength-bar")
                yield Static("Minimum 12 characters and score ≥ 3 required.", id="setup-strength-hint")

            with Horizontal(id="setup-checkbox-container"):
                yield Checkbox("", id="setup-checkbox", value=False)
                yield Label(
                    "I understand that a lost passphrase means permanent data loss and cannot be recovered.",
                    id="setup-checkbox-label",
                )

            yield Static("Creating vault...", id="setup-status")

            with Horizontal(classes="setup-buttons"):
                yield Button("Create vault", variant="primary", id="setup-submit-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#setup-passphrase", Input).focus()

    @on(events.Click, "#setup-checkbox-label")
    def _on_checkbox_label_click(self) -> None:
        cb = self.query_one("#setup-checkbox", Checkbox)
        cb.value = not cb.value

    @on(Input.Changed, "#setup-passphrase")
    @on(Input.Changed, "#setup-confirm")
    @on(Checkbox.Changed, "#setup-checkbox")
    def _validate_inputs(self) -> None:
        pw = self.query_one("#setup-passphrase", Input).value or ""
        confirm = self.query_one("#setup-confirm", Input).value or ""
        checkbox = self.query_one("#setup-checkbox", Checkbox)
        ack = bool(checkbox.value)

        res = safe_zxcvbn(pw)
        score = int(res.get("score", 0))
        length = len(pw)

        # Requirements: at least 12 characters and score >= 3
        is_length_ok = length >= 12
        is_score_ok = score >= 3
        is_match = (pw == confirm) and (length > 0)

        bar_widget = self.query_one("#setup-strength-bar", Static)
        hint_widget = self.query_one("#setup-strength-hint", Static)
        submit_btn = self.query_one("#setup-submit-btn", Button)

        bar_widget.update(self._render_strength_bar(score, length))

        if length == 0:
            hint_widget.update("[#71717a]Minimum 12 characters and score ≥ 3 required.[/]")
        elif not is_length_ok:
            hint_widget.update(f"[#f87171]Requires at least 12 characters ({length}/12)[/]")
        elif not is_score_ok:
            feedback = res.get("feedback", {}).get("warning") or "Passphrase is too predictable."
            hint_widget.update(f"[#94a3b8]Strength: {self._score_label(score)} (score {score}/4) · {feedback}[/]")
        elif not is_match:
            hint_widget.update("[#94a3b8]Passphrases do not match yet[/]")
        else:
            hint_widget.update(f"[#38bdf8]Strong passphrase ({self._score_label(score)}) and match confirmed[/]")

        can_submit = is_length_ok and is_score_ok and is_match and ack
        submit_btn.disabled = not can_submit

    def _score_label(self, score: int) -> str:
        labels = ["Very Weak", "Weak", "Fair", "Strong", "Very Strong"]
        return labels[min(max(score, 0), 4)]

    def _render_strength_bar(self, score: int, length: int) -> Text:
        t = Text()
        t.append("Strength: ", style="bold #94a3b8")
        if length == 0:
            t.append("░░░░░░░░░░░░", style="dim #52525b")
            t.append(" (empty)", style="dim #71717a")
            return t

        bars = [
            ("███░░░░░░░░░", "#f87171"),  # 0
            ("████░░░░░░░░", "#f87171"),  # 1
            ("██████░░░░░░", "#e4e4e7"),  # 2
            ("█████████░░░", "#38bdf8"),  # 3
            ("████████████", "#38bdf8"),  # 4
        ]
        bar_str, color = bars[min(max(score, 0), 4)]
        t.append(bar_str, style=color)
        t.append(f" {self._score_label(score)}", style=f"bold {color}")
        t.append(f" ({length} chars)", style="dim #71717a")
        return t

    @on(Button.Pressed, "#setup-submit-btn")
    def _on_submit(self) -> None:
        pw = self.query_one("#setup-passphrase", Input).value or ""
        self.query_one("#setup-submit-btn", Button).disabled = True
        status = self.query_one("#setup-status", Static)
        status.styles.display = "block"
        status.update("[bold #38bdf8]Creating vault...[/]")
        self.run_worker(self._do_setup(pw), group="vault-setup")

    async def _do_setup(self, pw: str) -> None:
        vault = get_vault()
        try:
            # 1. Create vault
            await asyncio.to_thread(vault.create_vault, pw)

            # 2. If unencrypted data exists, migrate and verify
            status = self.query_one("#setup-status", Static)
            if vault.has_unencrypted_data():
                status.update("[bold #38bdf8]Migrating plaintext data into vault...[/]")
                res = await asyncio.to_thread(vault.migrate_legacy_data)
                if not res.success:
                    raise MigrationVerificationError("Migration verification failed")

            self.notify("Vault created successfully.", title="Create vault", severity="information")
            self.dismiss(True)
        except Exception as exc:
            status = self.query_one("#setup-status", Static)
            status.update(f"[bold #f87171]Setup Error: {exc}[/]")
            self.query_one("#setup-submit-btn", Button).disabled = False


class VaultUnlockModal(ModalScreen[bool]):
    """Modal dialog for unlocking existing encrypted vault."""

    DEFAULT_CSS = """
    VaultUnlockModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #vault-unlock-dialog {
        width: 58;
        height: auto;
        border: round #27272a;
        background: #111115;
        padding: 1 2;
    }
    #vault-unlock-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    #vault-unlock-desc {
        color: #a1a1aa;
        margin-bottom: 1;
    }
    #unlock-passphrase {
        background: #09090b;
        border: round #27272a;
        color: #ffffff;
        margin-bottom: 0;
    }
    #unlock-passphrase:focus {
        border: round #38bdf8;
    }
    #unlock-error {
        color: #f87171;
        min-height: 1;
        margin-top: 1;
    }
    .unlock-buttons {
        width: 100%;
        align: right middle;
        height: auto;
        margin-top: 1;
    }
    #unlock-forgot-btn {
        margin-right: 2;
        color: #94a3b8;
    }
    """

    BINDINGS = [
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def action_quit_app(self) -> None:
        self.app.exit()

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-unlock-dialog"):
            yield Label("Unlock vault", id="vault-unlock-title")
            yield Static("Enter your master passphrase to decrypt your workspace and credentials.", id="vault-unlock-desc")
            yield Input(placeholder="Master passphrase", password=True, id="unlock-passphrase")
            yield Static("", id="unlock-error")
            with Horizontal(classes="unlock-buttons"):
                yield Button("Forgot passphrase", id="unlock-forgot-btn")
                yield Button("Unlock", variant="primary", id="unlock-submit-btn")

    def on_mount(self) -> None:
        self.query_one("#unlock-passphrase", Input).focus()

    @on(Input.Submitted, "#unlock-passphrase")
    @on(Button.Pressed, "#unlock-submit-btn")
    def _on_unlock(self) -> None:
        pw = self.query_one("#unlock-passphrase", Input).value or ""
        if not pw:
            self.query_one("#unlock-error", Static).update("[#f87171]Please enter your master passphrase.[/]")
            return

        self.query_one("#unlock-submit-btn", Button).disabled = True
        self.query_one("#unlock-error", Static).update("[dim #94a3b8]Unlocking vault...[/]")
        self.run_worker(self._do_unlock(pw), group="vault-unlock")

    async def _do_unlock(self, pw: str) -> None:
        vault = get_vault()
        err_widget = self.query_one("#unlock-error", Static)
        inp = self.query_one("#unlock-passphrase", Input)

        try:
            await asyncio.to_thread(vault.unlock, pw)
            self.dismiss(True)
        except InvalidPassphraseError:
            err_widget.update("[bold #f87171]Incorrect passphrase. Please try again.[/]")
            inp.value = ""
            inp.focus()
            self.query_one("#unlock-submit-btn", Button).disabled = False
        except TamperedDataError as exc:
            err_widget.update(f"[bold #f87171]Vault integrity error: {exc}[/]")
            self.query_one("#unlock-submit-btn", Button).disabled = False
        except Exception as exc:
            err_widget.update(f"[bold #f87171]Decryption failed: {exc}[/]")
            self.query_one("#unlock-submit-btn", Button).disabled = False

    @on(Button.Pressed, "#unlock-forgot-btn")
    def _on_forgot(self) -> None:
        self.app.push_screen(VaultWipeConfirmModal(), self._on_wipe_confirmed)

    def _on_wipe_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            vault = get_vault()
            vault.wipe()
            self.dismiss(False)
            self.app.push_screen(VaultSetupModal(), self._on_fresh_setup)

    def _on_fresh_setup(self, success: bool) -> None:
        if success:
            if hasattr(self.app, "_complete_startup"):
                self.run_worker(self.app._complete_startup(), group="startup")


class VaultWipeConfirmModal(ModalScreen[bool]):
    """Confirmation modal warning the user of permanent data loss before wiping vault."""

    DEFAULT_CSS = """
    VaultWipeConfirmModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #vault-wipe-dialog {
        width: 60;
        height: auto;
        border: round #27272a;
        background: #111115;
        padding: 1 2;
    }
    #vault-wipe-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    #vault-wipe-desc {
        color: #a1a1aa;
        margin-bottom: 1;
    }
    .wipe-buttons {
        width: 100%;
        align: right middle;
        height: auto;
        margin-top: 1;
    }
    #wipe-cancel-btn {
        margin-right: 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-wipe-dialog"):
            yield Label("Reset and wipe vault", id="vault-wipe-title")
            warning_text = (
                "Your vault is encrypted with Argon2id and XChaCha20-Poly1305.\n\n"
                "Because Rackhand does not store your passphrase, your data is "
                "mathematically unrecoverable without it.\n\n"
                "Resetting will permanently wipe your encrypted vault file and all stored "
                "settings, API keys, credentials, and backups, allowing you to start fresh.\n\n"
                "Are you sure you want to permanently wipe the vault?"
            )
            yield Static(warning_text, id="vault-wipe-desc")
            with Horizontal(classes="wipe-buttons"):
                yield Button("Cancel", id="wipe-cancel-btn")
                yield Button("Wipe Data & Start Fresh", variant="error", id="wipe-confirm-btn")

    @on(Button.Pressed, "#wipe-cancel-btn")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#wipe-confirm-btn")
    def _confirm(self) -> None:
        self.dismiss(True)


class VaultChangePassphraseModal(ModalScreen[bool]):
    """Modal for changing master passphrase and re-wrapping data key."""

    DEFAULT_CSS = """
    VaultChangePassphraseModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #vault-change-dialog {
        width: 64;
        height: auto;
        border: round #27272a;
        background: #111115;
        padding: 1 2;
    }
    #vault-change-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    .field-label {
        color: #94a3b8;
        margin-top: 1;
        margin-bottom: 0;
    }
    #change-current-pw, #change-new-pw, #change-confirm-pw {
        background: #09090b;
        border: round #27272a;
        color: #ffffff;
    }
    #change-current-pw:focus, #change-new-pw:focus, #change-confirm-pw:focus {
        border: round #38bdf8;
    }
    #change-strength-bar {
        margin-top: 1;
    }
    #change-status {
        color: #f87171;
        min-height: 1;
        margin-top: 1;
    }
    .change-buttons {
        width: 100%;
        align: right middle;
        height: auto;
        margin-top: 1;
    }
    #change-cancel-btn {
        margin-right: 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-change-dialog"):
            yield Label("Change master passphrase", id="vault-change-title")
            yield Label("Current Passphrase", classes="field-label")
            yield Input(placeholder="Current passphrase", password=True, id="change-current-pw")

            yield Label("New Passphrase", classes="field-label")
            yield Input(placeholder="New passphrase (min 12 chars, score ≥ 3)", password=True, id="change-new-pw")

            yield Label("Confirm New Passphrase", classes="field-label")
            yield Input(placeholder="Confirm new passphrase", password=True, id="change-confirm-pw")

            yield Static(self._render_strength_bar(0, 0), id="change-strength-bar")
            yield Static("", id="change-status")

            with Horizontal(classes="change-buttons"):
                yield Button("Cancel", id="change-cancel-btn")
                yield Button("Update Passphrase", variant="primary", id="change-submit-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#change-current-pw", Input).focus()

    def _render_strength_bar(self, score: int, length: int) -> Text:
        t = Text()
        t.append("Strength: ", style="bold #94a3b8")
        if length == 0:
            t.append("░░░░░░░░░░░░", style="dim #52525b")
            return t
        bars = ["███░░░░░░░░░", "████░░░░░░░░", "██████░░░░░░", "█████████░░░", "████████████"]
        colors = ["#f87171", "#f87171", "#e4e4e7", "#38bdf8", "#38bdf8"]
        t.append(bars[min(max(score, 0), 4)], style=colors[min(max(score, 0), 4)])
        t.append(f" ({length} chars)", style="dim #71717a")
        return t

    @on(Input.Changed, "#change-current-pw")
    @on(Input.Changed, "#change-new-pw")
    @on(Input.Changed, "#change-confirm-pw")
    def _validate(self) -> None:
        curr = self.query_one("#change-current-pw", Input).value or ""
        new = self.query_one("#change-new-pw", Input).value or ""
        confirm = self.query_one("#change-confirm-pw", Input).value or ""

        res = safe_zxcvbn(new)
        score = int(res.get("score", 0))
        length = len(new)

        self.query_one("#change-strength-bar", Static).update(self._render_strength_bar(score, length))

        can_submit = bool(curr) and (length >= 12) and (score >= 3) and (new == confirm)
        self.query_one("#change-submit-btn", Button).disabled = not can_submit

    @on(Button.Pressed, "#change-cancel-btn")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#change-submit-btn")
    def _on_submit(self) -> None:
        curr = self.query_one("#change-current-pw", Input).value or ""
        new = self.query_one("#change-new-pw", Input).value or ""

        self.query_one("#change-submit-btn", Button).disabled = True
        status = self.query_one("#change-status", Static)
        status.update("[bold #38bdf8]Updating passphrase...[/]")
        self.run_worker(self._do_change(curr, new), group="vault-change")

    async def _do_change(self, curr: str, new: str) -> None:
        vault = get_vault()
        status = self.query_one("#change-status", Static)
        try:
            await asyncio.to_thread(vault.change_passphrase, curr, new)
            self.notify("Vault master passphrase updated successfully.", title="Vault Updated", severity="information")
            self.dismiss(True)
        except InvalidPassphraseError:
            status.update("[bold #f87171]Current passphrase is incorrect.[/]")
            self.query_one("#change-submit-btn", Button).disabled = False
        except Exception as exc:
            status.update(f"[bold #f87171]Failed to change passphrase: {exc}[/]")
            self.query_one("#change-submit-btn", Button).disabled = False
