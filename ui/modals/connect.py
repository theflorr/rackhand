from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, Collapsible, Input, Label, OptionList, Select, Static,
    TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from models import (
    DeviceProfile, DeviceSession, SerialPortInfo, SerialSettings, SSHSettings,
    Transport, Vendor,
)
from ui.styles import MenuStatic, sel_value

BAUDS = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]


class ConnectScreen(ModalScreen[Optional[DeviceSession]]):
    """Connect to a device via serial console or SSH."""
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._ports: list[SerialPortInfo] = []
        self._profiles: list[DeviceProfile] = []
        self._form_visible = False

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="cn-dialog", classes="dialog"):
            yield MenuStatic(Text("CONNECT DEVICE", style="bold white"), classes="title")

            with Horizontal(id="cn-body"):

                # ── Left: detected & saved devices ─────────────────────────────
                with Vertical(id="cn-saved"):
                    yield OptionList(id="saved-list")
                    yield Button("+ New device", id="cn-new", classes="cn-new-btn")

                # ── Right: placeholder OR form ─────────────────────────────────
                with Vertical(id="cn-right"):

                    # Empty state — shown until user picks a device or hits New
                    yield Static(
                        "Select a device on the left",
                        id="cn-placeholder",
                    )

                    # Form — hidden by default
                    with VerticalScroll(id="cn-form"):
                        with TabbedContent(id="transport", initial="tab-serial"):

                            # ── Serial / Console ──────────────────────────────
                            with TabPane("Serial / Console", id="tab-serial"):

                                yield Label("Port", classes="cn-field-label")
                                with Horizontal(classes="cn-row"):
                                    yield Select(
                                        [], prompt="Scanning ports...", id="ser-port",
                                        allow_blank=True,
                                    )
                                    yield Button("Refresh", id="ser-refresh", classes="cn-btn-sm")

                                with Horizontal(classes="cn-row cn-baud-row"):
                                    with Vertical(classes="cn-col"):
                                        yield Label("Baud rate", classes="cn-field-label")
                                        yield Select(
                                            [(b, b) for b in BAUDS], value="9600",
                                            allow_blank=False, id="ser-baud",
                                        )
                                    yield Button("Auto-detect", id="ser-probe", classes="cn-btn-sm")

                                with Collapsible(
                                    title="Line settings  —  8N1, no flow control",
                                    collapsed=True,
                                    id="cn-line-settings",
                                ):
                                    with Horizontal(classes="cn-row"):
                                        with Vertical(classes="cn-col"):
                                            yield Label("Data bits", classes="cn-field-label")
                                            yield Select(
                                                [(str(i), str(i)) for i in (8, 7, 6, 5)],
                                                value="8", allow_blank=False, id="ser-data",
                                            )
                                        with Vertical(classes="cn-col"):
                                            yield Label("Parity", classes="cn-field-label")
                                            yield Select(
                                                [("None", "N"), ("Even", "E"), ("Odd", "O")],
                                                value="N", allow_blank=False, id="ser-parity",
                                            )
                                        with Vertical(classes="cn-col"):
                                            yield Label("Stop bits", classes="cn-field-label")
                                            yield Select(
                                                [("1", "1"), ("1.5", "1.5"), ("2", "2")],
                                                value="1", allow_blank=False, id="ser-stop",
                                            )
                                    with Horizontal(classes="cn-row"):
                                        with Vertical(classes="cn-col"):
                                            yield Label("Flow control", classes="cn-field-label")
                                            yield Select(
                                                [("None", "none"), ("RTS/CTS", "rtscts"),
                                                 ("XON/XOFF", "xonxoff")],
                                                value="none", allow_blank=False, id="ser-flow",
                                            )
                                        with Vertical(classes="cn-col"):
                                            yield Label("Line ending", classes="cn-field-label")
                                            yield Select(
                                                [("CR", "CR"), ("CRLF", "CRLF"), ("LF", "LF")],
                                                value="CR", allow_blank=False, id="ser-eol",
                                            )

                            # ── SSH ───────────────────────────────────────────
                            with TabPane("SSH", id="tab-ssh"):

                                with Horizontal(classes="cn-row"):
                                    with Vertical(id="cn-ssh-host-col"):
                                        yield Label("Host / IP address", classes="cn-field-label")
                                        yield Input(placeholder="10.10.1.1", id="ssh-host")
                                    with Vertical(id="cn-ssh-port-col"):
                                        yield Label("Port", classes="cn-field-label")
                                        yield Input("22", id="ssh-port", type="integer")

                                with Horizontal(classes="cn-row"):
                                    with Vertical(classes="cn-col"):
                                        yield Label("Username", classes="cn-field-label")
                                        yield Input(placeholder="admin", id="ssh-user")
                                    with Vertical(classes="cn-col"):
                                        yield Label("Authentication", classes="cn-field-label")
                                        yield Select(
                                            [("Password", "password"),
                                             ("Private key", "key"),
                                             ("SSH agent", "agent")],
                                            value="password", allow_blank=False, id="ssh-auth",
                                        )

                                yield Label("Password", id="ssh-secret-label", classes="cn-field-label")
                                yield Input(
                                    placeholder="not saved to disk",
                                    password=True, id="ssh-secret",
                                )
                                yield Label("Private key path", id="ssh-key-label", classes="cn-field-label")
                                yield Input(placeholder="~/.ssh/id_ed25519", id="ssh-key")

                                with Collapsible(title="Jump host & host key", collapsed=True):
                                    yield Label("Jump host (optional)", classes="cn-field-label")
                                    yield Input(placeholder="user@bastion:22", id="ssh-jump")
                                    yield Label("Host key policy", classes="cn-field-label")
                                    yield Select(
                                        [("Ask on first connect", "ask"),
                                         ("Trust on first use", "tofu"),
                                         ("Strict — must be in known_hosts", "strict")],
                                        value="ask", allow_blank=False, id="ssh-hostkey",
                                    )

                        # ── Enable password + save ─────────────────────────────
                        yield Label("Enable / privileged password", classes="cn-field-label")
                        yield Input(
                            placeholder="optional — not saved to disk",
                            password=True, id="enable",
                        )
                        yield Checkbox("Save to inventory", True, id="remember")

            # ── Status + footer ────────────────────────────────────────────────
            yield Static("", id="connect-status")

            with Horizontal(id="cn-footer"):
                yield Button("Cancel", id="cn-cancel", classes="cn-btn-cancel")
                yield Button("Connect", variant="primary", id="cn-connect")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._toggle_key_fields("password")
        self._show_form(False)   # start with placeholder visible

        self._profiles = await self.app.backend.load_profiles()  # type: ignore[attr-defined]
        self._ports = await self.app.backend.list_serial_ports()  # type: ignore[attr-defined]
        self._update_port_select()
        self._rebuild_device_list()

    # ── List population ────────────────────────────────────────────────────────

    def _rebuild_device_list(self) -> None:
        lst = self.query_one("#saved-list", OptionList)
        lst.clear_options()

        # ── Detected devices ──────────────────────────────────────────────
        lst.add_option(Option(Text(" DETECTED DEVICES", style="bold white"), disabled=True))
        if self._ports:
            for i, p in enumerate(self._ports):
                desc = p.description or "Serial console"
                if " (" in desc:
                    desc = desc.split(" (")[0]
                if len(desc) > 28:
                    desc = desc[:27] + "…"
                lst.add_option(Option(
                    Text.assemble(
                        (p.device, "bold #f1f5f9"), "\n",
                        (f"  {desc}", "dim #666666"),
                    ),
                    id=f"det_{i}",
                ))
        else:
            lst.add_option(Option(Text("   (no serial ports detected)", style="dim #444444"), disabled=True))

        # ── 1 line space between lists ────────────────────────────────────
        lst.add_option(Option(Text(""), disabled=True))

        # ── Saved devices ─────────────────────────────────────────────────
        lst.add_option(Option(Text(" SAVED DEVICES", style="bold white"), disabled=True))
        if self._profiles:
            for i, p in enumerate(self._profiles):
                addr = p.address
                if len(addr) > 28:
                    addr = addr[:27] + "…"
                lst.add_option(Option(
                    Text.assemble(
                        (p.name, "bold #f1f5f9"), "\n",
                        (f"  {addr}", "dim #666666"),
                    ),
                    id=f"p{i}",
                ))
        else:
            lst.add_option(Option(Text("   (no saved devices)", style="dim #444444"), disabled=True))

    def _update_port_select(self) -> None:
        sel = self.query_one("#ser-port", Select)
        keep = sel_value(sel)
        opts = [
            (f"{p.device}  —  {p.description}" if p.description else p.device, p.device)
            for p in self._ports
        ]
        sel.set_options(opts)
        if keep and keep in {v for _, v in opts}:
            sel.value = keep
        elif opts:
            sel.value = opts[0][1]

    # ── Visibility helpers ─────────────────────────────────────────────────────

    def _show_form(self, visible: bool) -> None:
        self._form_visible = visible
        self.query_one("#cn-placeholder").display = not visible
        self.query_one("#cn-form").display = visible
        self.query_one("#cn-connect").display = visible

    # ── Form reset (clean slate) ───────────────────────────────────────────────

    def _reset_form(self) -> None:
        """Reset all inputs to clean defaults on the Serial tab."""
        # Deselect in left list
        try:
            self.query_one("#saved-list", OptionList).highlighted = None
        except Exception:
            pass

        # Switch to Serial tab
        tabs = self.query_one("#transport", TabbedContent)
        tabs.active = "tab-serial"

        # Serial defaults
        port_sel = self.query_one("#ser-port", Select)
        if self._ports:
            port_sel.value = self._ports[0].device
        elif port_sel._options:
            port_sel.value = port_sel._options[0][1]
        else:
            port_sel.value = Select.BLANK

        self.query_one("#ser-baud", Select).value = "9600"
        self.query_one("#ser-data", Select).value = "8"
        self.query_one("#ser-parity", Select).value = "N"
        self.query_one("#ser-stop", Select).value = "1"
        self.query_one("#ser-flow", Select).value = "none"
        self.query_one("#ser-eol", Select).value = "CR"

        # SSH defaults
        self._reset_ssh_fields()

        # Common defaults
        self.query_one("#enable", Input).value = ""
        self.query_one("#remember", Checkbox).value = True
        self.query_one("#connect-status", Static).update("")

    def _reset_ssh_fields(self) -> None:
        self.query_one("#ssh-host", Input).value = ""
        self.query_one("#ssh-port", Input).value = "22"
        self.query_one("#ssh-user", Input).value = ""
        self.query_one("#ssh-auth", Select).value = "password"
        self.query_one("#ssh-secret", Input).value = ""
        self.query_one("#ssh-key", Input).value = ""
        self.query_one("#ssh-jump", Input).value = ""
        self.query_one("#ssh-hostkey", Select).value = "ask"
        self._toggle_key_fields("password")

    # ── Button handlers ────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cn-new")
    def _new_device(self) -> None:
        """Clear current selection and replace with a clean slate on Serial tab."""
        self._reset_form()
        self._show_form(True)
        try:
            self.query_one("#ser-port", Select).focus()
        except Exception:
            pass

    # ── Serial port handling ───────────────────────────────────────────────────

    @on(Button.Pressed, "#ser-refresh")
    def _refresh_pressed(self) -> None:
        self._refresh_ports()

    @work(exclusive=True, group="ports")
    async def _refresh_ports(self) -> None:
        try:
            self._ports = await self.app.backend.list_serial_ports()  # type: ignore[attr-defined]
            if self.is_mounted:
                self._update_port_select()
                self._rebuild_device_list()
            if not self._ports and self.is_mounted and self._form_visible:
                self.notify(
                    "No serial ports detected. Check the console cable and driver.",
                    severity="warning",
                )
        except Exception:
            pass

    @on(Button.Pressed, "#ser-probe")
    def _probe_pressed(self) -> None:
        port = sel_value(self.query_one("#ser-port", Select))
        if not port:
            self.notify("Pick a port first.", severity="warning")
            return
        self._probe_baud(port)

    @work(exclusive=True, group="probe")
    async def _probe_baud(self, port: str) -> None:
        """Silently probe baud rate — only updates the baud select, no noisy status."""
        status = self.query_one("#connect-status", Static)
        status.update(Text(f"Detecting baud on {port}...", style="dim #555555"))
        try:
            res = await self.app.backend.probe_device(port, transport=Transport.SERIAL)  # type: ignore[attr-defined]
            if res.ok and str(res.baud) in BAUDS:
                self.query_one("#ser-baud", Select).value = str(res.baud)
                status.update(Text(f"Baud detected: {res.baud}", style="dim #22c55e"))
            else:
                status.update(Text("Could not detect baud — set it manually.", style="dim #888888"))
        except Exception:
            status.update("")

    # ── SSH auth toggle ────────────────────────────────────────────────────────

    def _toggle_key_fields(self, auth: str) -> None:
        self.query_one("#ssh-secret-label").display = auth != "agent"
        self.query_one("#ssh-secret").display = auth != "agent"
        self.query_one("#ssh-key-label").display = auth == "key"
        self.query_one("#ssh-key").display = auth == "key"
        self.query_one("#ssh-secret-label", Label).update(
            "Key passphrase (optional)" if auth == "key" else "Password"
        )

    @on(Select.Changed, "#ssh-auth")
    def _auth_changed(self, ev: Select.Changed) -> None:
        if isinstance(ev.value, str):
            self._toggle_key_fields(ev.value)

    # ── Load device selection from left list ───────────────────────────────────

    @on(OptionList.OptionSelected, "#saved-list")
    def _pick_item(self, ev: OptionList.OptionSelected) -> None:
        opt_id = str(ev.option_id or "")
        if opt_id.startswith("det_"):
            idx = int(opt_id.split("_")[1])
            if idx < len(self._ports):
                self._load_detected_port(self._ports[idx])
        elif opt_id.startswith("p"):
            idx = int(opt_id[1:])
            if idx < len(self._profiles):
                self._load_saved_profile(self._profiles[idx])

    def _load_detected_port(self, port_info: SerialPortInfo) -> None:
        self.query_one("#connect-status", Static).update("")

        tabs = self.query_one("#transport", TabbedContent)
        tabs.active = "tab-serial"

        port_sel = self.query_one("#ser-port", Select)
        existing = {v for _, v in self._port_options()}
        if port_info.device not in existing:
            port_sel.set_options(self._port_options() + [(port_info.device, port_info.device)])
        port_sel.value = port_info.device

        # Default baud and 8N1
        self.query_one("#ser-baud", Select).value = "9600"
        self.query_one("#ser-data", Select).value = "8"
        self.query_one("#ser-parity", Select).value = "N"
        self.query_one("#ser-stop", Select).value = "1"
        self.query_one("#ser-flow", Select).value = "none"
        self.query_one("#ser-eol", Select).value = "CR"

        # Clean slate on SSH fields & enable
        self._reset_ssh_fields()
        self.query_one("#enable", Input).value = ""
        self._show_form(True)

    def _load_saved_profile(self, p: DeviceProfile) -> None:
        self.query_one("#connect-status", Static).update("")

        tabs = self.query_one("#transport", TabbedContent)
        if p.transport is Transport.SERIAL and p.serial:
            tabs.active = "tab-serial"
            port_sel = self.query_one("#ser-port", Select)
            existing = {v for _, v in self._port_options()}
            if p.serial.port not in existing:
                port_sel.set_options(self._port_options() + [(p.serial.port, p.serial.port)])
            port_sel.value = p.serial.port
            if str(p.serial.baud) in BAUDS:
                self.query_one("#ser-baud", Select).value = str(p.serial.baud)
            self._reset_ssh_fields()
        elif p.ssh:
            tabs.active = "tab-ssh"
            self.query_one("#ssh-host", Input).value = p.ssh.host
            self.query_one("#ssh-port", Input).value = str(p.ssh.port)
            self.query_one("#ssh-user", Input).value = p.ssh.username
            self.query_one("#ssh-auth", Select).value = p.ssh.auth
            self.query_one("#ssh-key", Input).value = p.ssh.key_path
            self._toggle_key_fields(p.ssh.auth)

        self._show_form(True)

    def _port_options(self) -> list[tuple[str, str]]:
        return [(p.device, p.device) for p in self._ports]

    # ── Connect ────────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#cn-connect")
    def _connect_pressed(self) -> None:
        q = self.query_one
        secrets: dict[str, str] = {"enable": q("#enable", Input).value}

        if q("#transport", TabbedContent).active == "tab-serial":
            port = sel_value(q("#ser-port", Select))
            if not port:
                self.notify("Choose a serial port.", severity="error")
                return
            ser = SerialSettings(
                port,
                int(sel_value(q("#ser-baud", Select)) or 9600),
                int(sel_value(q("#ser-data", Select)) or 8),
                sel_value(q("#ser-parity", Select)) or "N",
                sel_value(q("#ser-stop", Select)) or "1",
                sel_value(q("#ser-flow", Select)) or "none",
                sel_value(q("#ser-eol", Select)) or "CR",
            )
            profile = DeviceProfile(port, Transport.SERIAL, Vendor.AUTO, serial=ser)
        else:
            host = q("#ssh-host", Input).value.strip()
            user = q("#ssh-user", Input).value.strip()
            if not host or not user:
                self.notify("Host and username are required for SSH.", severity="error")
                return
            try:
                port_n = int(q("#ssh-port", Input).value or 22)
            except ValueError:
                self.notify("SSH port must be a number.", severity="error")
                return
            ssh = SSHSettings(
                host, port_n, user,
                sel_value(q("#ssh-auth", Select)) or "password",
                q("#ssh-key", Input).value.strip(),
                q("#ssh-jump", Input).value.strip(),
                sel_value(q("#ssh-hostkey", Select)) or "ask",
            )
            profile = DeviceProfile(host, Transport.SSH, Vendor.AUTO, ssh=ssh)
            secrets["password"] = q("#ssh-secret", Input).value

        self._do_connect(profile, secrets)

    @work(exclusive=True, group="connect")
    async def _do_connect(self, profile: DeviceProfile, secrets: dict[str, str]) -> None:
        status = self.query_one("#connect-status", Static)
        btn = self.query_one("#cn-connect", Button)
        btn.disabled = True
        status.update(Text(f"Connecting to {profile.address}...", style="dim #888888"))
        try:
            session = await self.app.backend.connect(profile, secrets)  # type: ignore[attr-defined]
        except Exception as exc:
            status.update(Text(f"Connection failed: {exc}", style="#ef4444"))
            btn.disabled = False
            return
        if session.hostname:
            profile.name = session.hostname
        if self.query_one("#remember", Checkbox).value:
            await self.app.backend.save_profile(profile)  # type: ignore[attr-defined]
        self.dismiss(session)
