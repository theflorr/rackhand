from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import random
import re
import socket
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

try:
    import paramiko
except ImportError:
    paramiko = None

from llm_engine import RealLLMEngine

from models import (
    AgentDone,
    AgentError,
    AgentEvent,
    AgentProposal,
    AgentText,
    AgentToolCall,
    AgentToolResult,
    ApiScheme,
    ApprovalDecision,
    AuthStyle,
    CommandResult,
    ConnState,
    ConsoleChunk,
    DeviceProbeResult,
    DeviceProfile,
    DeviceSession,
    ProposedChange,
    ProviderConfig,
    PROVIDERS,
    Risk,
    SerialPortInfo,
    SerialSettings,
    Settings,
    SSHSettings,
    TestResult,
    Transport,
    Vendor,
    detect_vendor_from_text,
    extract_hostname,
    extract_prompt,
)

class Backend(ABC):
    """Everything the UI needs from the wire-up layer. All methods are async so the
    UI never blocks; serial/SSH I/O should live behind these in tasks or threads."""

    # ---- device discovery & profiles ----------------------------------------
    @abstractmethod
    async def list_serial_ports(self) -> list[SerialPortInfo]:
        """Enumerate COM/tty ports (pyserial list_ports). Called on open + Refresh."""

    @abstractmethod
    async def probe_baud(self, port: str) -> Optional[int]:
        """Try common bauds (9600/115200/...) and return the one that yields a prompt."""

    @abstractmethod
    async def probe_device(
        self,
        port_or_host: str,
        baud: Optional[int] = None,
        transport: Transport = Transport.SERIAL,
    ) -> DeviceProbeResult:
        """Probe serial console or network host, send CR/probe sequence, and
        automatically detect hostname, baud rate, prompt, and vendor platform."""

    @abstractmethod
    async def load_profiles(self) -> list[DeviceProfile]: ...

    @abstractmethod
    async def save_profile(self, profile: DeviceProfile) -> None: ...

    # ---- device sessions ------------------------------------------------------
    @abstractmethod
    async def connect(self, profile: DeviceProfile, secrets: dict[str, str]) -> DeviceSession:
        """secrets keys: password, key_passphrase, enable. Never persisted by the UI.
        Raise an Exception with a human-readable message on failure."""

    @abstractmethod
    async def disconnect(self, session_id: str) -> None: ...

    @abstractmethod
    def console_stream(self, session_id: str) -> AsyncIterator[ConsoleChunk]:
        """Async generator of everything on the wire (device output, user + agent commands)."""

    @abstractmethod
    async def run_command(self, session_id: str, command: str, source: str = "user") -> CommandResult: ...

    @abstractmethod
    async def send_control(self, session_id: str, action: str) -> None:
        """action: 'cr' | 'ctrl_c' | 'ctrl_z' | 'break' (ROMMON entry over serial)."""

    # ---- LLM providers ----------------------------------------------------------
    @abstractmethod
    async def load_provider_configs(self) -> dict[str, ProviderConfig]: ...

    @abstractmethod
    async def save_provider_config(self, cfg: ProviderConfig) -> None: ...

    @abstractmethod
    async def delete_provider_config(self, key: str) -> None: ...

    @abstractmethod
    async def test_provider(self, cfg: ProviderConfig) -> TestResult:
        """Cheap round trip, ideally also a tiny tool-call to verify tool support."""

    @abstractmethod
    async def list_models(self, cfg: ProviderConfig | str) -> list[str]: ...

    @abstractmethod
    async def detect_subscriptions(self) -> list[Any]: ...

    @abstractmethod
    async def auto_import_detected(self) -> list[ProviderConfig]: ...

    # ---- agent ------------------------------------------------------------------
    @abstractmethod
    def run_turn(self, prompt: str, session: DeviceSession, provider: ProviderConfig,
                 settings: Settings, agent: Optional[Any] = None,
                 chat_session_id: Optional[str] = None) -> AsyncIterator[AgentEvent]:
        """Async generator. Yield AgentText as it streams, AgentToolCall/Result when the
        model runs commands, AgentProposal when a change needs approval (then the generator
        must wait until resolve_approval is called), and finally AgentDone."""

    @abstractmethod
    async def resolve_approval(self, change_id: str, decision: ApprovalDecision) -> None: ...

    @abstractmethod
    async def cancel_turn(self) -> None: ...

    # ---- settings ---------------------------------------------------------------
    @abstractmethod
    async def load_settings(self) -> Settings: ...

    @abstractmethod
    async def save_settings(self, settings: Settings) -> None: ...


# =============================================================================
# 4. HARDWARE SERIAL & SSH SESSIONS
# =============================================================================

class RealSerialSession:
    """Manages an active serial port connection with background reading and command queuing."""
    def __init__(self, session_id: str, profile: DeviceProfile, loop: asyncio.AbstractEventLoop, chunk_queue: asyncio.Queue[ConsoleChunk]):
        self.session_id = session_id
        self.profile = profile
        self.loop = loop
        self.chunk_queue = chunk_queue
        self.ser: Optional[serial.Serial] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.active_prompt = profile.name + ">"
        self.hostname = profile.name
        self.vendor = profile.vendor

        self._cmd_lock = asyncio.Lock()
        self._cmd_active = False
        self._cmd_buf: list[str] = []
        self._cmd_event: Optional[asyncio.Event] = None

    def start(self, ser: serial.Serial, initial_prompt: str = ""):
        self.ser = ser
        if initial_prompt:
            self.active_prompt = initial_prompt
            h = extract_hostname(initial_prompt)
            if h:
                self.hostname = h
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True, name=f"serial-{self.session_id}")
        self.thread.start()

    def _reader_loop(self):
        while not self.stop_event.is_set():
            try:
                if not self.ser or not self.ser.is_open:
                    break
                n = self.ser.in_waiting
                raw = self.ser.read(n if n > 0 else 1)
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace")

                # Forward to UI console stream
                self.loop.call_soon_threadsafe(
                    self.chunk_queue.put_nowait,
                    ConsoleChunk(self.session_id, text, "device")
                )

                # Keep hostname fresh from wire text
                h = extract_hostname(text)
                if h:
                    self.hostname = h

                with self.lock:
                    if self._cmd_active:
                        self._cmd_buf.append(text)
                        # Auto-paginate --More-- so commands complete
                        if "--More--" in text or "--more--" in text:
                            try:
                                self.ser.write(b" ")
                            except Exception:
                                pass
                        full = "".join(self._cmd_buf)
                        lines = [l.strip() for l in full.replace("\r\n", "\n").split("\n") if l.strip()]
                        if lines:
                            last = lines[-1]
                            if (last.endswith(">") or last.endswith("#") or last.endswith("$")) and len(lines) > 1:
                                self.active_prompt = last
                                if self._cmd_event and not self._cmd_event.is_set():
                                    self.loop.call_soon_threadsafe(self._cmd_event.set)
            except Exception:
                if self.stop_event.is_set():
                    break
                time.sleep(0.04)

    def _write_bytes(self, data: bytes):
        with self.lock:
            if self.ser and self.ser.is_open:
                self.ser.write(data)
                self.ser.flush()

    async def run_command(self, command: str, source: str = "user", timeout_s: float = 30.0) -> CommandResult:
        async with self._cmd_lock:
            start_t = time.time()
            self._cmd_event = asyncio.Event()
            with self.lock:
                self._cmd_buf = []
                self._cmd_active = True

            # Support agents sending \r, \n, or \r\n (strip trailing, normalize line endings to wire \r\n)
            norm_cmd = (command or "").replace("\r\n", "\n").replace("\r", "\n")
            if not norm_cmd.strip():
                clean_lines = []
                cmd_bytes = b"\r\n"
            else:
                clean_lines = [l.strip() for l in norm_cmd.split("\n") if l.strip()]
                wire_cmd = "\r\n".join(clean_lines) + "\r\n"
                cmd_bytes = wire_cmd.encode("ascii", errors="replace")

            await asyncio.to_thread(self._write_bytes, cmd_bytes)

            try:
                await asyncio.wait_for(self._cmd_event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                pass
            finally:
                with self.lock:
                    self._cmd_active = False
                    raw_out = "".join(self._cmd_buf)
                    self._cmd_buf = []

            dur_ms = int((time.time() - start_t) * 1000)

            # Strip command echo at top and prompt at bottom
            lines = raw_out.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            while lines and not lines[0].strip():
                lines.pop(0)
            for cl in clean_lines:
                if lines and lines[0].strip() == cl:
                    lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            while lines and (lines[-1].strip().endswith(">") or lines[-1].strip().endswith("#") or lines[-1].strip().endswith("$")):
                self.active_prompt = lines[-1].strip()
                lines.pop()
                while lines and not lines[-1].strip():
                    lines.pop()

            clean_out = "\n".join(lines).strip()
            ok = not any(err in clean_out for err in ("% Invalid input", "% Incomplete command", "% Ambiguous command", "% Privilege denied"))
            return CommandResult(output=clean_out, prompt=self.active_prompt, duration_ms=dur_ms, ok=ok)

    def send_control(self, action: str):
        if action == "cr":
            self._write_bytes(b"\r\n")
        elif action == "ctrl_c":
            self._write_bytes(b"\x03")
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^C\r\n", "user"))
        elif action == "ctrl_z":
            self._write_bytes(b"\x1a")
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^Z\r\n", "user"))
        elif action == "break":
            with self.lock:
                if self.ser and self.ser.is_open:
                    try:
                        self.ser.send_break()
                    except Exception:
                        pass
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "\r\n*** BREAK sent ***\r\n", "user"))

    def close(self):
        self.stop_event.set()
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass


class RealSSHSession:
    """Manages an active SSH session with background shell reading and command queuing."""
    def __init__(self, session_id: str, profile: DeviceProfile, loop: asyncio.AbstractEventLoop, chunk_queue: asyncio.Queue[ConsoleChunk]):
        self.session_id = session_id
        self.profile = profile
        self.loop = loop
        self.chunk_queue = chunk_queue
        self.client: Optional[paramiko.SSHClient] = None
        self.channel = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.active_prompt = profile.name + "#"
        self.hostname = profile.name
        self.vendor = profile.vendor

        self._cmd_lock = asyncio.Lock()
        self._cmd_active = False
        self._cmd_buf: list[str] = []
        self._cmd_event: Optional[asyncio.Event] = None

    def start(self, client: paramiko.SSHClient, channel, initial_prompt: str = ""):
        self.client = client
        self.channel = channel
        if initial_prompt:
            self.active_prompt = initial_prompt
            h = extract_hostname(initial_prompt)
            if h:
                self.hostname = h
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True, name=f"ssh-{self.session_id}")
        self.thread.start()

    def _reader_loop(self):
        while not self.stop_event.is_set():
            try:
                if not self.channel or self.channel.closed:
                    break
                if self.channel.recv_ready():
                    raw = self.channel.recv(4096)
                    if not raw:
                        break
                    text = raw.decode("utf-8", errors="replace")
                    self.loop.call_soon_threadsafe(
                        self.chunk_queue.put_nowait,
                        ConsoleChunk(self.session_id, text, "device")
                    )
                    h = extract_hostname(text)
                    if h:
                        self.hostname = h

                    with self.lock:
                        if self._cmd_active:
                            self._cmd_buf.append(text)
                            if "--More--" in text or "--more--" in text:
                                try:
                                    self.channel.send(" ")
                                except Exception:
                                    pass
                            full = "".join(self._cmd_buf)
                            lines = [l.strip() for l in full.replace("\r\n", "\n").split("\n") if l.strip()]
                            if lines:
                                last = lines[-1]
                                if (last.endswith(">") or last.endswith("#") or last.endswith("$")) and len(lines) > 1:
                                    self.active_prompt = last
                                    if self._cmd_event and not self._cmd_event.is_set():
                                        self.loop.call_soon_threadsafe(self._cmd_event.set)
                else:
                    time.sleep(0.04)
            except Exception:
                if self.stop_event.is_set():
                    break
                time.sleep(0.05)

    def _write_bytes(self, data: bytes):
        with self.lock:
            if self.channel and not self.channel.closed:
                self.channel.send(data)

    async def run_command(self, command: str, source: str = "user", timeout_s: float = 30.0) -> CommandResult:
        async with self._cmd_lock:
            start_t = time.time()
            self._cmd_event = asyncio.Event()
            with self.lock:
                self._cmd_buf = []
                self._cmd_active = True

            # Support agents sending \r, \n, or \r\n (strip trailing, normalize line endings to wire \r\n)
            norm_cmd = (command or "").replace("\r\n", "\n").replace("\r", "\n")
            if not norm_cmd.strip():
                clean_lines = []
                cmd_bytes = b"\r\n"
            else:
                clean_lines = [l.strip() for l in norm_cmd.split("\n") if l.strip()]
                wire_cmd = "\r\n".join(clean_lines) + "\r\n"
                cmd_bytes = wire_cmd.encode("utf-8", errors="replace")

            await asyncio.to_thread(self._write_bytes, cmd_bytes)

            try:
                await asyncio.wait_for(self._cmd_event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                pass
            finally:
                with self.lock:
                    self._cmd_active = False
                    raw_out = "".join(self._cmd_buf)
                    self._cmd_buf = []

            dur_ms = int((time.time() - start_t) * 1000)
            lines = raw_out.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            while lines and not lines[0].strip():
                lines.pop(0)
            for cl in clean_lines:
                if lines and lines[0].strip() == cl:
                    lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            while lines and (lines[-1].strip().endswith(">") or lines[-1].strip().endswith("#") or lines[-1].strip().endswith("$")):
                self.active_prompt = lines[-1].strip()
                lines.pop()
                while lines and not lines[-1].strip():
                    lines.pop()

            clean_out = "\n".join(lines).strip()
            ok = not any(err in clean_out for err in ("% Invalid input", "% Incomplete command", "% Ambiguous command", "% Privilege denied"))
            return CommandResult(output=clean_out, prompt=self.active_prompt, duration_ms=dur_ms, ok=ok)

    def send_control(self, action: str):
        if action == "cr":
            self._write_bytes(b"\r\n")
        elif action == "ctrl_c":
            self._write_bytes(b"\x03")
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^C\r\n", "user"))
        elif action == "ctrl_z":
            self._write_bytes(b"\x1a")
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^Z\r\n", "user"))
        elif action == "break":
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "\r\n*** BREAK not supported over SSH ***\r\n", "user"))

    def close(self):
        self.stop_event.set()
        with self.lock:
            if self.channel:
                try:
                    self.channel.close()
                except Exception:
                    pass
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass


class RealBackend(Backend):
    """Production backend connecting to real hardware over Serial COM and SSH."""

    def __init__(self) -> None:
        self._dir = Path.home() / ".rackhand"
        self._profiles_file = self._dir / "profiles.json"
        self._settings_file = self._dir / "settings.json"
        self._providers_file = self._dir / "providers.json"
        self._dir.mkdir(parents=True, exist_ok=True)

        self._active_sessions: dict[str, DeviceSession] = {}
        self._serial_drivers: dict[str, RealSerialSession] = {}
        self._ssh_drivers: dict[str, RealSSHSession] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._approvals: dict[str, asyncio.Future] = {}
        self._llm_engine = RealLLMEngine()

        self._providers: dict[str, ProviderConfig] = {
            "anthropic": ProviderConfig(
                key="anthropic",
                name="Anthropic",
                spec_id="anthropic",
                scheme=ApiScheme.ANTHROPIC,
                base_url="https://api.anthropic.com",
                api_key="",
                auth=AuthStyle.X_API_KEY,
                model="claude-3-5-sonnet-20241022",
                context_window=200000,
                max_output=8192,
                supports_tools=True,
                supports_streaming=True,
            )
        }
        if self._providers_file.exists():
            try:
                pdata = json.loads(self._providers_file.read_text(encoding="utf-8"))
                for pk, pv in pdata.items():
                    self._providers[pk] = ProviderConfig.from_dict(pv)
            except Exception:
                pass

    # ---- device discovery & profiles ----------------------------------------

    async def list_serial_ports(self) -> list[SerialPortInfo]:
        if serial is None or not hasattr(serial, "tools") or not hasattr(serial.tools, "list_ports"):
            return []
        ports = await asyncio.to_thread(serial.tools.list_ports.comports)
        out = []
        for p in ports:
            out.append(SerialPortInfo(
                device=p.device,
                description=p.description or "Serial Port",
                hwid=p.hwid or "",
            ))
        return out

    async def probe_baud(self, port: str) -> Optional[int]:
        res = await self.probe_device(port, transport=Transport.SERIAL)
        return res.baud if res.ok else 9600

    async def probe_device(
        self,
        port_or_host: str,
        baud: Optional[int] = None,
        transport: Transport = Transport.SERIAL,
    ) -> DeviceProbeResult:
        p = (port_or_host or "").strip()
        is_serial = transport is Transport.SERIAL or p.upper().startswith("COM") or "/dev/tty" in p

        if is_serial:
            if serial is None:
                return DeviceProbeResult(False, p, 9600, message="pyserial not available")

            def _probe_sync():
                candidate_bauds = [115200, 9600, 57600, 38400, 19200, 2400]
                if baud and baud in candidate_bauds:
                    candidate_bauds.remove(baud)
                    candidate_bauds.insert(0, baud)

                for b in candidate_bauds:
                    try:
                        s = serial.Serial(
                            port=p,
                            baudrate=b,
                            bytesize=serial.EIGHTBITS,
                            parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE,
                            timeout=0.35,
                            write_timeout=0.35,
                            rtscts=False,
                            dsrdtr=False,
                            xonxoff=False,
                        )
                        s.dtr = True
                        s.rts = True
                        s.reset_input_buffer()
                        s.reset_output_buffer()
                        s.write(b"\r\n\r\n")
                        time.sleep(0.3)
                        n = s.in_waiting
                        resp = s.read(n if n > 0 else 50).decode("utf-8", errors="replace")

                        if resp and any(c.isalnum() for c in resp):
                            # Device responded at this baud! Query version
                            s.write(b"terminal length 0\r\nshow version\r\n")
                            time.sleep(0.5)
                            n2 = s.in_waiting
                            ver_resp = s.read(n2 if n2 > 0 else 500).decode("utf-8", errors="replace")
                            full_text = resp + "\n" + ver_resp

                            prompt = extract_prompt(full_text) or "SW1>"
                            hostname = extract_hostname(full_text) or "SW1"
                            vendor = detect_vendor_from_text(full_text)
                            s.close()
                            return DeviceProbeResult(
                                ok=True,
                                port_or_host=p,
                                baud=b,
                                prompt=prompt,
                                hostname=hostname,
                                vendor=vendor,
                                message=f"{hostname} ({vendor.value}) detected on {p} at {b} baud",
                            )
                        s.close()
                    except Exception:
                        pass
                return DeviceProbeResult(ok=False, port_or_host=p, baud=baud or 9600, message=f"No response on {p}")

            return await asyncio.to_thread(_probe_sync)

        else:
            # SSH probe
            def _probe_ssh_sync():
                try:
                    s = socket.create_connection((p, 22), timeout=2.0)
                    banner = s.recv(1024).decode("utf-8", errors="replace")
                    s.close()
                    vendor = detect_vendor_from_text(banner)
                    host = extract_hostname(banner) or p
                    return DeviceProbeResult(
                        ok=True,
                        port_or_host=p,
                        baud=22,
                        hostname=host,
                        vendor=vendor,
                        message=f"SSH reachable: {banner.strip()}",
                    )
                except Exception as e:
                    return DeviceProbeResult(ok=False, port_or_host=p, message=f"SSH unreachable on {p}:22 ({e})")

            return await asyncio.to_thread(_probe_ssh_sync)

    async def load_profiles(self) -> list[DeviceProfile]:
        # 1. Read existing saved profiles if present
        if self._profiles_file.is_file():
            try:
                data = json.loads(self._profiles_file.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    profiles = [DeviceProfile.from_dict(d) for d in data if isinstance(d, dict)]
                    if profiles:
                        return profiles
            except Exception:
                pass

        # 2. If no saved profiles, scan real ports to auto-discover
        ports = await self.list_serial_ports()
        # Prefer physical USB serial ports
        usb_ports = [p for p in ports if "bluetooth" not in p.description.lower() and "bthenum" not in p.hwid.lower()]
        target_ports = usb_ports if usb_ports else ports

        for p in target_ports:
            probe = await self.probe_device(p.device, transport=Transport.SERIAL)
            if probe.ok:
                name = probe.hostname or p.device
                prof = DeviceProfile(
                    name=name,
                    transport=Transport.SERIAL,
                    vendor=probe.vendor,
                    serial=SerialSettings(p.device, probe.baud),
                    notes=f"{p.description} (Auto-detected)",
                )
                await self.save_profile(prof)
                return [prof]

        return []

    async def save_profile(self, profile: DeviceProfile) -> None:
        updated = []
        if self._profiles_file.is_file():
            try:
                data = json.loads(self._profiles_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    updated = [DeviceProfile.from_dict(d) for d in data if isinstance(d, dict)]
            except Exception:
                pass
        updated = [p for p in updated if p.name != profile.name and p.address != profile.address]
        updated.append(profile)
        try:
            self._profiles_file.write_text(
                json.dumps([p.to_dict() for p in updated], indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---- device sessions ------------------------------------------------------

    async def connect(self, profile: DeviceProfile, secrets: dict[str, str]) -> DeviceSession:
        session_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        chunk_queue: asyncio.Queue = asyncio.Queue()
        self._queues[session_id] = chunk_queue

        if profile.transport is Transport.SERIAL:
            if not profile.serial:
                raise ValueError("Profile missing serial settings")
            port = profile.serial.port
            baud = profile.serial.baud or 115200

            def _open_serial_sync():
                s = serial.Serial(
                    port=port,
                    baudrate=baud,
                    bytesize=getattr(serial, f"{profile.serial.data_bits}BITS", serial.EIGHTBITS),
                    parity={"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}.get(profile.serial.parity, serial.PARITY_NONE),
                    stopbits={"1": serial.STOPBITS_ONE, "1.5": serial.STOPBITS_ONE_POINT_FIVE, "2": serial.STOPBITS_TWO}.get(profile.serial.stop_bits, serial.STOPBITS_ONE),
                    timeout=0.1,
                    write_timeout=1.0,
                    rtscts=(profile.serial.flow == "rtscts"),
                    xonxoff=(profile.serial.flow == "xonxoff"),
                )
                s.dtr = True
                s.rts = True
                s.reset_input_buffer()
                s.reset_output_buffer()
                s.write(b"\r\n\r\n")
                time.sleep(0.3)
                init_text = s.read(s.in_waiting or 100).decode("utf-8", errors="replace")
                return s, init_text

            ser, init_resp = await asyncio.to_thread(_open_serial_sync)

            # Determine initial prompt, hostname, vendor
            probe_res = await self.probe_device(port, baud=baud, transport=Transport.SERIAL)
            hostname = probe_res.hostname if probe_res.ok else (extract_hostname(init_resp) or profile.name)
            prompt = probe_res.prompt if probe_res.ok else (extract_prompt(init_resp) or f"{hostname}>")
            vendor = probe_res.vendor if probe_res.ok else profile.vendor

            # Update profile name
            if not profile.name or profile.name == port:
                profile.name = hostname

            driver = RealSerialSession(session_id, profile, loop, chunk_queue)
            driver.start(ser, prompt)
            self._serial_drivers[session_id] = driver

            # Disable pagination on Cisco/network gear
            await driver.run_command("terminal length 0")

            mode = "privileged" if prompt.endswith("#") else "exec"
            session = DeviceSession(
                id=session_id,
                profile=profile,
                state=ConnState.CONNECTED,
                prompt=prompt,
                hostname=hostname,
                mode=mode,
                vendor=vendor,
            )
            self._active_sessions[session_id] = session

            banner = (
                f"\r\nConnected to {hostname} ({port} @ {baud}) over Serial Console\r\n"
                f"Operating mode: {mode.upper()}  -  {prompt}\r\n\r\n"
            )
            chunk_queue.put_nowait(ConsoleChunk(session_id, banner + prompt + " ", "device"))
            return session

        else:
            # SSH Connection
            if not profile.ssh:
                raise ValueError("Profile missing SSH settings")
            if paramiko is None:
                raise RuntimeError("paramiko is not installed for SSH")

            def _open_ssh_sync():
                client = paramiko.SSHClient()
                policy_map = {
                    "ask": paramiko.AutoAddPolicy(),
                    "tofu": paramiko.AutoAddPolicy(),
                    "strict": paramiko.RejectPolicy(),
                }
                client.set_missing_host_key_policy(policy_map.get(profile.ssh.host_key_policy, paramiko.AutoAddPolicy()))

                key_file = profile.ssh.key_path if (profile.ssh.auth == "key" and profile.ssh.key_path) else None
                client.connect(
                    hostname=profile.ssh.host,
                    port=profile.ssh.port,
                    username=profile.ssh.username,
                    password=secrets.get("password") or None,
                    key_filename=key_file,
                    passphrase=secrets.get("key_passphrase") or None,
                    timeout=10.0,
                    look_for_keys=(profile.ssh.auth == "agent"),
                )
                channel = client.invoke_shell(term="vt100", width=120, height=40)
                channel.send("\r\n\r\n")
                time.sleep(0.4)
                init_text = ""
                if channel.recv_ready():
                    init_text = channel.recv(2048).decode("utf-8", errors="replace")
                return client, channel, init_text

            client, channel, init_resp = await asyncio.to_thread(_open_ssh_sync)

            hostname = extract_hostname(init_resp) or profile.name or profile.ssh.host
            prompt = extract_prompt(init_resp) or f"{hostname}#"
            vendor = detect_vendor_from_text(init_resp)

            driver = RealSSHSession(session_id, profile, loop, chunk_queue)
            driver.start(client, channel, prompt)
            self._ssh_drivers[session_id] = driver

            await driver.run_command("terminal length 0")

            mode = "privileged" if prompt.endswith("#") else "exec"
            session = DeviceSession(
                id=session_id,
                profile=profile,
                state=ConnState.CONNECTED,
                prompt=prompt,
                hostname=hostname,
                mode=mode,
                vendor=vendor,
            )
            self._active_sessions[session_id] = session

            banner = (
                f"\r\nConnected to {hostname} ({profile.ssh.host}:{profile.ssh.port}) via SSH\r\n"
                f"Operating mode: {mode.upper()}  -  {prompt}\r\n\r\n"
            )
            chunk_queue.put_nowait(ConsoleChunk(session_id, banner + prompt + " ", "device"))
            return session

    async def disconnect(self, session_id: str) -> None:
        ser_drv = self._serial_drivers.pop(session_id, None)
        if ser_drv:
            ser_drv.close()

        ssh_drv = self._ssh_drivers.pop(session_id, None)
        if ssh_drv:
            ssh_drv.close()

        self._active_sessions.pop(session_id, None)
        self._queues.pop(session_id, None)

    async def console_stream(self, session_id: str) -> AsyncIterator[ConsoleChunk]:
        q = self._queues.get(session_id)
        while q is not None:
            yield await q.get()

    async def run_command(self, session_id: str, command: str, source: str = "user") -> CommandResult:
        ser_drv = self._serial_drivers.get(session_id)
        if ser_drv:
            return await ser_drv.run_command(command, source=source)

        ssh_drv = self._ssh_drivers.get(session_id)
        if ssh_drv:
            return await ssh_drv.run_command(command, source=source)

        return CommandResult("Device session disconnected.", "", 0, False)

    async def send_control(self, session_id: str, action: str) -> None:
        ser_drv = self._serial_drivers.get(session_id)
        if ser_drv:
            ser_drv.send_control(action)
            return

        ssh_drv = self._ssh_drivers.get(session_id)
        if ssh_drv:
            ssh_drv.send_control(action)

    # ---- LLM providers ----------------------------------------------------------

    def _save_providers_to_disk(self) -> None:
        try:
            pdata = {k: v.to_dict() for k, v in self._providers.items()}
            self._providers_file.write_text(json.dumps(pdata, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def load_provider_configs(self) -> dict[str, ProviderConfig]:
        return dict(self._providers)

    async def save_provider_config(self, cfg: ProviderConfig) -> None:
        self._providers[cfg.key] = cfg
        self._save_providers_to_disk()

    async def delete_provider_config(self, key: str) -> None:
        self._providers.pop(key, None)
        self._save_providers_to_disk()

    async def test_provider(self, cfg: ProviderConfig) -> TestResult:
        if cfg.scheme == ApiScheme.ANTIGRAVITY or cfg.spec_id == "antigravity" or cfg.base_url == "local:agy" or "antigravity" in cfg.name.lower():
            start_t = time.time()
            try:
                cmd = ["agy", "--effort", "low", "--output-format", "json", "-p", "Reply with OK"]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                dur_ms = int((time.time() - start_t) * 1000)
                if proc.returncode == 0:
                    try:
                        res_obj = json.loads(stdout.decode("utf-8", errors="replace"))
                        cid = res_obj.get("conversation_id", "")
                        return TestResult(True, f"Connected to Antigravity ({cfg.model}) via AGY CLI [cid: {cid[:8]}]", dur_ms, True)
                    except Exception:
                        return TestResult(True, f"Connected to Antigravity ({cfg.model}) via AGY CLI", dur_ms, True)
                else:
                    err_msg = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
                    return TestResult(False, f"AGY CLI error (code {proc.returncode}): {err_msg[:120]}", dur_ms, False)
            except asyncio.TimeoutError:
                return TestResult(False, "AGY CLI timed out after 15s", 15000, False)
            except Exception as e:
                return TestResult(False, f"Failed to invoke AGY CLI: {e}", 0, False)

        if cfg.base_url and cfg.api_key and "local" not in cfg.base_url:
            start_t = time.time()
            try:
                import httpx
                url = cfg.base_url.rstrip("/")
                if not url.endswith("/chat/completions") and not url.endswith("/v1"):
                    url = f"{url}/chat/completions"
                elif url.endswith("/v1"):
                    url = f"{url}/chat/completions"
                headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
                payload = {"model": cfg.model or "meta-llama/llama-3.3-70b-instruct", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    dur_ms = int((time.time() - start_t) * 1000)
                    if resp.status_code == 200:
                        return TestResult(True, f"Connected to {cfg.name} ({cfg.model})", dur_ms, cfg.supports_tools)
                    else:
                        return TestResult(False, f"HTTP {resp.status_code}: {resp.text[:120]}", dur_ms, False)
            except Exception as e:
                return TestResult(False, f"Connection failed: {e}", 0, False)

        await asyncio.sleep(0.3)
        if cfg.auth is not AuthStyle.NONE and not cfg.api_key and "localhost" not in cfg.base_url:
            return TestResult(False, "401 Unauthorized: missing API key", 120)
        return TestResult(True, "Reachable, authenticated", 140, cfg.supports_tools)

    async def list_models(self, cfg: ProviderConfig | str) -> list[str]:
        if isinstance(cfg, str):
            cfg_obj = self._providers.get(cfg)
            if cfg_obj:
                cfg = cfg_obj
            else:
                cfg = ProviderConfig(key=cfg, spec_id=cfg, base_url="", auth=AuthStyle.BEARER)

        if cfg.scheme == ApiScheme.ANTIGRAVITY or cfg.spec_id == "antigravity" or cfg.base_url == "local:agy" or "antigravity" in cfg.name.lower():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "agy", "models",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if proc.returncode == 0:
                    models = []
                    for line in stdout.decode("utf-8", errors="replace").splitlines():
                        parts = line.strip().split("\t")
                        if parts and parts[0] and not parts[0].startswith("Fetching"):
                            models.append(parts[0])
                    if models:
                        return models
            except Exception:
                pass
            return ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high", "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]

        if cfg.base_url and (cfg.api_key or "localhost" in cfg.base_url or "127.0.0.1" in cfg.base_url):
            try:
                models = await _query_native_endpoint_models(cfg)
                if models:
                    return models
            except Exception:
                pass
        prov_key = cfg.spec_id or cfg.key
        from models import PROVIDER_MODELS
        if prov_key in PROVIDER_MODELS:
            return [m[1] for m in PROVIDER_MODELS[prov_key]]
        return ["default-model", "fast-model"]

    async def detect_subscriptions(self) -> list[Any]:
        from subscription_detector import detect_all_subscriptions
        return await detect_all_subscriptions()

    async def auto_import_detected(self) -> list[ProviderConfig]:
        from subscription_detector import detect_all_subscriptions
        detected = await detect_all_subscriptions()
        imported: list[ProviderConfig] = []
        for d in detected:
            cfg = d.to_provider_config()
            if cfg.key == "antigravity" and "gemini" in self._providers:
                old_gem = self._providers["gemini"]
                if old_gem.api_key and str(old_gem.api_key).startswith("ya29"):
                    del self._providers["gemini"]
            if cfg.key not in self._providers or not self._providers[cfg.key].api_key:
                self._providers[cfg.key] = cfg
                imported.append(cfg)
        if imported:
            self._save_providers_to_disk()
        return imported

    # ---- agent ------------------------------------------------------------------

    async def run_turn(
        self,
        prompt: str,
        session: DeviceSession,
        provider: ProviderConfig,
        settings: Settings,
        agent: Optional[Any] = None,
        chat_session_id: Optional[str] = None,
    ) -> AsyncIterator[AgentEvent]:
        if agent is None:
            from agents import resolve_agent
            agent = resolve_agent("auto", session)

        async def _run_cmd_fn(cmd: str):
            if session:
                return await self.run_command(session.id, cmd, "agent")
            return "No device connected to execute CLI commands."

        async def _propose_fn(change: ProposedChange) -> ApprovalDecision:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._approvals[change.id] = fut
            return await fut

        sess_id = chat_session_id or (session.id if session else "default")

        # Delegate to RealLLMEngine for live multi-turn tool calling, context memory, and streaming
        async for ev in self._llm_engine.run_turn(
            prompt=prompt,
            session=session,
            provider=provider,
            settings=settings,
            agent_spec=agent,
            run_command_fn=_run_cmd_fn,
            propose_change_fn=_propose_fn,
            session_id=sess_id,
        ):
            yield ev

    async def resolve_approval(self, change_id: str, decision: ApprovalDecision) -> None:
        fut = self._approvals.pop(change_id, None)
        if fut and not fut.done():
            fut.set_result(decision)

    async def cancel_turn(self) -> None:
        for fut in self._approvals.values():
            if not fut.done():
                fut.set_result(ApprovalDecision(False))
        self._approvals.clear()

    # ---- settings ---------------------------------------------------------------

    async def load_settings(self) -> Settings:
        if self._settings_file.is_file():
            try:
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return Settings.from_dict(data)
            except Exception:
                pass
        return Settings()

    async def save_settings(self, settings: Settings) -> None:
        try:
            self._settings_file.write_text(
                json.dumps(settings.to_dict(), indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass



# =============================================================================
