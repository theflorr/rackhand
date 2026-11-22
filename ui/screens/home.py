from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label, OptionList, Static
from textual.widgets.option_list import Option

from backend import Backend
from models import DeviceProfile, DeviceSession, ProviderConfig, SCHEME_LABELS, Settings
from ui.modals import ConnectScreen, ProvidersScreen, SettingsModal
from ui.styles import BANNER_ART, build_logo, pill

class HomeScreen(Screen):
    BINDINGS = [
        Binding("d", "device", "Device"),
        Binding("m", "model", "Model"),
        Binding("o", "open", "Open workspace"),
        Binding("s", "settings", "Settings"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="home"):
            with Horizontal(id="home-header"):
                yield Static("", id="home-logo")
                yield Static("", id="home-nav")
            with Horizontal(id="cards"):
                with Vertical(classes="card", id="card-left"):
                    yield Static("", id="card-device")
                    with Horizontal(classes="card-actions"):
                        yield Button("Manage devices", variant="primary", id="h-device")
                        yield Button("Quick connect", id="h-quick-connect")
                with Vertical(id="home-vdivider"):
                    pass
                with Vertical(classes="card", id="card-right"):
                    yield Static("", id="card-model")
                    yield Button("Configure model", variant="primary", id="h-model")
            yield Static("", id="home-statusline")
            yield Button("Open workspace", variant="primary", id="h-open", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_cards()

    def on_screen_resume(self) -> None:
        self.refresh_cards()

    def refresh_cards(self) -> None:
        app = self.app
        sessions = app.sessions  # type: ignore[attr-defined]
        prov = app.provider  # type: ignore[attr-defined]

        # logo + nav header
        logo = Text()
        logo.append("RACKHAND", style="bold white")
        logo.append("  agentic network configuration", style="dim")
        self.query_one("#home-logo", Static).update(logo)

        nav = Text()
        nav.append("d", style="bold white")
        nav.append(" device  ", style="dim")
        nav.append("m", style="bold white")
        nav.append(" model  ", style="dim")
        nav.append("o", style="bold white")
        nav.append(" open  ", style="dim")
        nav.append("s", style="bold white")
        nav.append(" settings  ", style="dim")
        nav.append("q", style="bold white")
        nav.append(" quit", style="dim")
        self.query_one("#home-nav", Static).update(nav)

        dev = Text()
        dev.append("devices\n", style="dim")
        if sessions:
            for s in sessions:
                dev.append(f"{s.hostname or s.profile.name}\n", style="bold white")
                dev.append(f"  {s.profile.transport.value.upper()}  {s.profile.address}\n", style="white")
                dev.append(f"  {s.mode}  {s.vendor.value}\n", style="dim")
        else:
            dev.append("no active session\n", style="dim")
            dev.append("Press 'd' or Connect to attach a network device\n", style="dim")

        mod = Text()
        mod.append("model\n", style="dim")
        if prov:
            mod.append(f"{prov.name}\n", style="bold white")
            mod.append(f"  {prov.model}\n", style="white")
            mod.append(f"  {SCHEME_LABELS.get(prov.scheme, prov.scheme)}  tools={'on' if prov.supports_tools else 'off'}", style="dim")
        else:
            mod.append("not configured\n", style="dim")
            mod.append("pick a provider from the list", style="dim")

        # bottom status line
        ready = sessions and prov
        status = Text()
        if ready:
            status.append("ready  ", style="dim")
            status.append(f"{sessions[0].hostname}  {prov.model}", style="white")  # type: ignore[index]
        else:
            missing = []
            if not sessions:
                missing.append("device")
            if not prov:
                missing.append("model")
            status.append("waiting for: " + "  +  ".join(missing), style="dim")
        self.query_one("#home-statusline", Static).update(status)

        self.query_one("#card-device", Static).update(dev)
        self.query_one("#card-model", Static).update(mod)
        self.query_one("#h-open", Button).disabled = not ready

    @on(Button.Pressed, "#h-quick-connect")
    def _quick_connect(self) -> None:
        self.run_worker(self._do_quick_connect(), group="quick-connect")

    async def _do_quick_connect(self) -> None:
        profiles = await self.app.backend.load_profiles()  # type: ignore[attr-defined]
        target = profiles[0] if profiles else None
        if target:
            self.notify(f"Connecting to {target.name}...", title="Quick Connect")
            s = await self.app.backend.connect(target, {})  # type: ignore[attr-defined]
            self.app.sessions.append(s)  # type: ignore[attr-defined]
            self.app.active_id = s.id  # type: ignore[attr-defined]
            self.refresh_cards()

    def action_device(self) -> None:
        self.app.push_screen(ConnectScreen(), self._got_session)

    def _got_session(self, s: Optional[DeviceSession]) -> None:
        if s:
            self.app.sessions.append(s)  # type: ignore[attr-defined]
            self.app.active_id = s.id  # type: ignore[attr-defined]
        self.refresh_cards()

    def action_model(self) -> None:
        prov = self.app.provider  # type: ignore[attr-defined]
        self.app.push_screen(ProvidersScreen(prov.key if prov else None), self._got_provider)

    def _got_provider(self, cfg: Optional[ProviderConfig]) -> None:
        if cfg:
            self.app.provider = cfg  # type: ignore[attr-defined]
        self.refresh_cards()

    def action_settings(self) -> None:
        self.app.push_screen(SettingsModal(self.app.settings), self._got_settings)  # type: ignore[attr-defined]

    def _got_settings(self, s: Optional[Settings]) -> None:
        if s:
            self.app.apply_settings(s)  # type: ignore[attr-defined]

    def action_open(self) -> None:
        if self.query_one("#h-open", Button).disabled:
            self.notify("Connect a device and choose a model first.", severity="warning")
            return
        self.app.switch_screen(WorkspaceScreen())

    @on(Button.Pressed, "#h-device")
    def _p1(self) -> None:
        self.action_device()

    @on(Button.Pressed, "#h-model")
    def _p2(self) -> None:
        self.action_model()

    @on(Button.Pressed, "#h-open")
    def _p3(self) -> None:
        self.action_open()


# =============================================================================
# 7. STYLES + APP
# =============================================================================
