"""Update notification and auto-update modal dialog."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ProgressBar, Static

from updater import ReleaseInfo, apply_windows_update_and_restart, download_update


class UpdateModal(ModalScreen[bool]):
    """Modal dialog prompting the operator to update Rackhand."""
    
    DEFAULT_CSS = """
    UpdateModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    #update-dialog {
        width: 60;
        height: auto;
        border: round #38bdf8;
        background: #111113;
        padding: 1 2;
    }
    #update-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    #update-meta {
        color: #a1a1aa;
        margin-bottom: 1;
    }
    #update-notes {
        color: #71717a;
        margin-bottom: 1;
        max-height: 5;
    }
    #update-status {
        color: #38bdf8;
        margin-bottom: 1;
    }
    #update-progress {
        display: none;
        margin-bottom: 1;
    }
    .update-buttons {
        width: 100%;
        align: right middle;
        height: auto;
    }
    .update-buttons Button {
        margin-left: 1;
    }
    """
    
    BINDINGS = [Binding("escape", "dismiss_modal", "Dismiss")]

    def __init__(self, current_version: str, release: ReleaseInfo) -> None:
        super().__init__()
        self.current_version = current_version
        self.release = release
        self.downloaded_path: Optional[Path] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="update-dialog"):
            title = Text()
            title.append("› ", style="bold #38bdf8")
            title.append("New Update Available", style="bold #ffffff")
            yield Static(title, id="update-title")

            meta = Text()
            meta.append("Installed: ", style="dim #71717a")
            meta.append(f"{self.current_version}  ", style="bold #e4e4e7")
            meta.append("→  Latest: ", style="dim #71717a")
            meta.append(f"{self.release.title}\n", style="bold #38bdf8")
            if self.release.published_at:
                meta.append(f"Released: {self.release.published_at}  ·  ", style="dim #71717a")
            if self.release.asset_size:
                size_mb = self.release.asset_size / (1024 * 1024)
                meta.append(f"Size: {size_mb:.1f} MB", style="dim #71717a")
            yield Static(meta, id="update-meta")

            if self.release.body.strip():
                snippet = self.release.body.strip()[:200]
                if len(self.release.body.strip()) > 200:
                    snippet += "..."
                yield Static(snippet, id="update-notes")

            yield Static("", id="update-status")
            yield ProgressBar(total=100, show_eta=False, id="update-progress")

            with Horizontal(classes="update-buttons"):
                yield Button("Later", id="btn-later", variant="default")
                yield Button("Update Now", id="btn-update", variant="primary")

    @on(Button.Pressed, "#btn-later")
    def action_dismiss_modal(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-update")
    def _start_update(self) -> None:
        btn_update = self.query_one("#btn-update", Button)
        btn_later = self.query_one("#btn-later", Button)

        if btn_update.label == "Restart Now" and self.downloaded_path:
            apply_windows_update_and_restart(self.downloaded_path)
            return

        if not self.release.asset_download_url:
            # Fallback to browser if no direct binary asset
            import webbrowser
            webbrowser.open(self.release.html_url)
            self.dismiss(True)
            return

        btn_update.disabled = True
        btn_later.disabled = True
        self.query_one("#update-progress", ProgressBar).styles.display = "block"
        self._perform_download()

    @work(exclusive=True, group="auto-updater")
    async def _perform_download(self) -> None:
        status_lbl = self.query_one("#update-status", Static)
        prog_bar = self.query_one("#update-progress", ProgressBar)
        btn_update = self.query_one("#btn-update", Button)
        btn_later = self.query_one("#btn-later", Button)

        status_lbl.update("Downloading latest release...")
        
        target_dir = Path(os.environ.get("TEMP", ".")) / "rackhand_update"
        target_file = target_dir / "rackhand_new.exe"

        def on_prog(ratio: float, downloaded: int, total: int) -> None:
            pct = int(ratio * 100)
            self.app.call_from_thread(lambda: prog_bar.update(progress=pct))
            dl_mb = downloaded / (1024 * 1024)
            tot_mb = total / (1024 * 1024)
            self.app.call_from_thread(
                lambda: status_lbl.update(f"Downloading... {pct}% ({dl_mb:.1f} MB / {tot_mb:.1f} MB)")
            )

        success = await download_update(self.release.asset_download_url, target_file, on_prog)

        if success:
            self.downloaded_path = target_file
            status_lbl.update(Text("✓ Download complete! Click Restart Now to finish.", style="bold #38bdf8"))
            btn_update.label = "Restart Now"
            btn_update.disabled = False
            btn_later.label = "Close"
            btn_later.disabled = False
        else:
            status_lbl.update(Text("Download failed. Opening release in browser...", style="bold #f87171"))
            import webbrowser
            webbrowser.open(self.release.html_url)
            btn_later.label = "Close"
            btn_later.disabled = False
