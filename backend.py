from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import random
import re
import socket
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ui.messages import ChatMessage

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

logger = logging.getLogger("rackhand.backend")

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
    ChatSession,
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
    ensure_agy_in_path,
    extract_hostname,
    extract_prompt,
    find_agy_executable,
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
    async def generate_session_title(self, provider: ProviderConfig, prompt: str) -> Optional[str]: ...

    @abstractmethod
    async def detect_subscriptions(self) -> list[Any]: ...

    @abstractmethod
    async def auto_import_detected(self) -> list[ProviderConfig]: ...

    # ---- agent ------------------------------------------------------------------
    @abstractmethod
    def run_turn(self, prompt: str, session: DeviceSession, provider: ProviderConfig,
                 settings: Settings, agent: Optional[Any] = None,
                 chat_session_id: Optional[str] = None,
                 is_plan_mode: bool = False) -> AsyncIterator[AgentEvent]:
        """Async generator. Yield AgentText as it streams, AgentToolCall/Result when the
        model runs commands, AgentProposal when a change needs approval (then the generator
        must wait until resolve_approval is called), and finally AgentDone."""

    @abstractmethod
    async def resolve_approval(self, change_id: str, decision: ApprovalDecision) -> None: ...

    @abstractmethod
    async def cancel_turn(self) -> None: ...

    def inject_mid_turn_prompt(self, session_id: str, prompt: str) -> bool:
        """Inject a user prompt mid-turn (e.g. via Alt+M)."""
        return False

    @abstractmethod
    async def compact_session(
        self,
        session_id: str,
        provider: ProviderConfig,
        session: Optional[DeviceSession] = None,
        agent: Optional[Any] = None,
        is_plan_mode: bool = False,
        fallback_transcript: str = "",
    ) -> str:
        """Compact conversation history into a structured checkpoint summary and reset memory starting point."""

    @abstractmethod
    def clear_session_memory(self, session_id: str) -> None:
        """Clear context memory for the given session."""

    @abstractmethod
    def clone_session_memory(self, source_session_id: str, target_session_id: str) -> None:
        """Clone conversation context memory from one session to another."""

    @abstractmethod
    def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Get raw messages stored in context memory for the session."""

    @abstractmethod
    def set_session_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Set context memory messages for a session."""

    @abstractmethod
    async def load_chat_sessions(self) -> tuple[list[ChatSession], Optional[str]]:
        """Load saved chat sessions and the last active session ID.
        Returns (sessions_list, active_session_id)."""

    @abstractmethod
    async def load_chat_session_details(self, session_id: str) -> tuple[list[ChatMessage], list[dict[str, Any]]]:
        """Load UI chat messages and engine memory messages for a session.
        Returns (ui_messages, memory_messages)."""

    @abstractmethod
    async def save_chat_session(
        self,
        session: ChatSession,
        ui_messages: list[ChatMessage],
        memory_messages: Optional[list[dict[str, Any]]] = None,
        is_active: bool = False,
    ) -> None:
        """Save a chat session's metadata, UI messages, and context memory to disk."""

    @abstractmethod
    async def delete_chat_session_persisted(self, session_id: str) -> None:
        """Delete persisted session file and remove from session index."""

    @abstractmethod
    async def save_session_index(self, sessions: list[ChatSession], active_session_id: Optional[str]) -> None:
        """Update the saved session index order and active session ID."""

    @abstractmethod
    async def append_session_transcript(self, session: ChatSession, entry: str, settings: Optional[Settings] = None) -> None:
        """Append an audit log entry to the session transcript on disk if keep_transcripts is enabled."""

    # ---- settings ---------------------------------------------------------------
    @abstractmethod
    async def load_settings(self) -> Settings: ...

    @abstractmethod
    async def save_settings(self, settings: Settings) -> None: ...


# =============================================================================
# 4. HARDWARE SERIAL & SSH SESSIONS
# =============================================================================

class _BaseShellSession:
    """Shared background-reader and command-execution logic for Serial and SSH sessions."""

    _ERROR_MARKERS = (
        "% Invalid input",
        "% Incomplete command",
        "% Ambiguous command",
        "% Privilege denied",
    )

    def __init__(
        self,
        session_id: str,
        profile: DeviceProfile,
        loop: asyncio.AbstractEventLoop,
        chunk_queue: asyncio.Queue[Optional[ConsoleChunk]],
        default_prompt_suffix: str = ">",
    ) -> None:
        self.session_id = session_id
        self.profile = profile
        self.loop = loop
        self.chunk_queue = chunk_queue
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.active_prompt = profile.name + default_prompt_suffix
        self.hostname = profile.name
        self.vendor = profile.vendor

        self._cmd_lock = asyncio.Lock()
        self._cmd_active = False
        self._cmd_buf: list[str] = []
        self._cmd_expected_echo: list[str] = []
        self._cmd_event: Optional[asyncio.Event] = None

    def _handle_wire_text(self, text: str) -> None:
        """Process decoded wire text, stream to UI, and detect command completion."""
        try:
            self.loop.call_soon_threadsafe(
                self.chunk_queue.put_nowait,
                ConsoleChunk(self.session_id, text, "device"),
            )
        except RuntimeError:
            pass

        with self.lock:
            p = extract_prompt(text)
            if p:
                h = extract_hostname(p)
                if h:
                    self.hostname = h

            if not self._cmd_active:
                return

            self._cmd_buf.append(text)
            if "--More--" in text or "--more--" in text:
                self._send_pagination_space()

            full = "".join(self._cmd_buf)
            # Only consider a prompt complete if the chunk does not end with a newline
            # (a live CLI prompt sits at the end of the line waiting for input).
            if full.endswith("\n"):
                return
            lines = [ln.strip() for ln in full.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
            if not lines:
                return
            last = lines[-1]
            # Wait for at least one line beyond echoed command(s) unless no echo is expected
            if self._cmd_expected_echo and len(lines) <= 1 and last == self._cmd_expected_echo[0]:
                return
            if extract_prompt(last):
                self.active_prompt = last
                if self._cmd_event and not self._cmd_event.is_set():
                    try:
                        self.loop.call_soon_threadsafe(self._cmd_event.set)
                    except RuntimeError:
                        pass

    def _send_pagination_space(self) -> None:
        pass

    def _write_bytes(self, data: bytes) -> None:
        raise NotImplementedError

    def _encode_command(self, wire_cmd: str) -> bytes:
        return wire_cmd.encode("utf-8", errors="replace")

    async def run_command(self, command: str, source: str = "user", timeout_s: float = 30.0) -> CommandResult:
        async with self._cmd_lock:
            start_t = time.time()
            self._cmd_event = asyncio.Event()
            norm_cmd = (command or "").replace("\r\n", "\n").replace("\r", "\n")
            if not norm_cmd.strip():
                clean_lines: list[str] = []
                cmd_bytes = b"\r\n"
            else:
                clean_lines = [ln.strip() for ln in norm_cmd.split("\n") if ln.strip()]
                wire_cmd = "\r\n".join(clean_lines) + "\r\n"
                cmd_bytes = self._encode_command(wire_cmd)

            with self.lock:
                self._cmd_buf = []
                self._cmd_expected_echo = clean_lines
                self._cmd_active = True

            await asyncio.to_thread(self._write_bytes, cmd_bytes)

            try:
                await asyncio.wait_for(self._cmd_event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                pass
            finally:
                with self.lock:
                    self._cmd_active = False
                    self._cmd_expected_echo = []
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
            while lines and extract_prompt(lines[-1].strip()):
                self.active_prompt = lines[-1].strip()
                lines.pop()
                while lines and not lines[-1].strip():
                    lines.pop()

            clean_out = "\n".join(lines).strip()
            ok = not any(err in clean_out for err in self._ERROR_MARKERS)
            return CommandResult(output=clean_out, prompt=self.active_prompt, duration_ms=dur_ms, ok=ok)

    def send_control(self, action: str) -> None:
        if action == "cr":
            self._write_bytes(b"\r\n")
        elif action == "ctrl_c":
            self._write_bytes(b"\x03")
            try:
                self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^C\r\n", "user"))
            except RuntimeError:
                pass
        elif action == "ctrl_z":
            self._write_bytes(b"\x1a")
            try:
                self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "^Z\r\n", "user"))
            except RuntimeError:
                pass
        elif action == "break":
            self._send_break()

    def _send_break(self) -> None:
        pass


class RealSerialSession(_BaseShellSession):
    """Manages an active serial port connection with background reading and command queuing."""

    def __init__(self, session_id: str, profile: DeviceProfile, loop: asyncio.AbstractEventLoop, chunk_queue: asyncio.Queue[Optional[ConsoleChunk]]):
        super().__init__(session_id, profile, loop, chunk_queue, default_prompt_suffix=">")
        self.ser: Optional[serial.Serial] = None

    def start(self, ser: serial.Serial, initial_prompt: str = "") -> None:
        self.ser = ser
        if initial_prompt:
            self.active_prompt = initial_prompt
            h = extract_hostname(initial_prompt)
            if h:
                self.hostname = h
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True, name=f"serial-{self.session_id}")
        self.thread.start()

    def _reader_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.ser or not self.ser.is_open:
                    break
                n = self.ser.in_waiting
                raw = self.ser.read(n if n > 0 else 1)
                if not raw:
                    continue
                self._handle_wire_text(raw.decode("utf-8", errors="replace"))
            except Exception:
                if self.stop_event.is_set():
                    break
                time.sleep(0.04)

    def _send_pagination_space(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b" ")
            except Exception:
                pass

    def _write_bytes(self, data: bytes) -> None:
        with self.lock:
            if self.ser and self.ser.is_open:
                self.ser.write(data)
                self.ser.flush()

    def _encode_command(self, wire_cmd: str) -> bytes:
        return wire_cmd.encode("ascii", errors="replace")

    def _send_break(self) -> None:
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.send_break()
                except Exception:
                    pass
        try:
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "\r\n*** BREAK sent ***\r\n", "user"))
        except RuntimeError:
            pass

    def close(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass


class RealSSHSession(_BaseShellSession):
    """Manages an active SSH session with background shell reading and command queuing."""

    def __init__(self, session_id: str, profile: DeviceProfile, loop: asyncio.AbstractEventLoop, chunk_queue: asyncio.Queue[Optional[ConsoleChunk]]):
        super().__init__(session_id, profile, loop, chunk_queue, default_prompt_suffix="#")
        self.client: Optional[paramiko.SSHClient] = None
        self.channel = None

    def start(self, client: paramiko.SSHClient, channel, initial_prompt: str = "") -> None:
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

    def _reader_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.channel or self.channel.closed:
                    break
                if self.channel.recv_ready():
                    raw = self.channel.recv(4096)
                    if not raw:
                        break
                    self._handle_wire_text(raw.decode("utf-8", errors="replace"))
                else:
                    time.sleep(0.04)
            except Exception:
                if self.stop_event.is_set():
                    break
                time.sleep(0.05)

    def _send_pagination_space(self) -> None:
        if self.channel and not self.channel.closed:
            try:
                self.channel.send(" ")
            except Exception:
                pass

    def _write_bytes(self, data: bytes) -> None:
        with self.lock:
            if self.channel and not self.channel.closed:
                self.channel.send(data)

    def _send_break(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.chunk_queue.put_nowait, ConsoleChunk(self.session_id, "\r\n*** BREAK not supported over SSH ***\r\n", "user"))
        except RuntimeError:
            pass

    def close(self) -> None:
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
        self._sessions_index_file = self._dir / "sessions.json"
        self._sessions_dir = self._dir / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        ensure_agy_in_path()

        from vault import get_vault
        self.vault = get_vault()
        self._profiles_cache: list[DeviceProfile] = []

        self._active_sessions: dict[str, DeviceSession] = {}
        self._serial_drivers: dict[str, RealSerialSession] = {}
        self._ssh_drivers: dict[str, RealSSHSession] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._approvals: dict[str, asyncio.Future] = {}
        self._llm_engine = RealLLMEngine()
        from usage_tracker import UsageTracker
        self.usage_tracker = UsageTracker.get_instance()

        self._providers: dict[str, ProviderConfig] = {}
        if self.vault.is_unlocked():
            self.reload_from_vault()
        elif self._providers_file.exists():
            try:
                pdata = json.loads(self._providers_file.read_text(encoding="utf-8"))
                for pk, pv in pdata.items():
                    self._providers[pk] = ProviderConfig.from_dict(pv)
            except Exception:
                pass

    def reload_from_vault(self) -> None:
        """Reload providers and settings into memory from decrypted vault."""
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            pdata = self.vault.get_providers()
            for pk, pv in pdata.items():
                self._providers[pk] = ProviderConfig.from_dict(pv)

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

    def get_profiles_sync(self) -> list[DeviceProfile]:
        """Synchronously return cached device profiles or inventory for quick completions."""
        if hasattr(self, "_profiles_cache") and self._profiles_cache:
            return list(self._profiles_cache)
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            vdata = self.vault.get_profiles()
            if vdata:
                res = [DeviceProfile.from_dict(d) for d in vdata if isinstance(d, dict)]
                self._profiles_cache = res
                return res
        elif hasattr(self, "_profiles_file") and self._profiles_file.is_file():
            try:
                data = json.loads(self._profiles_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    res = [DeviceProfile.from_dict(d) for d in data if isinstance(d, dict)]
                    self._profiles_cache = res
                    return res
            except Exception:
                pass
        return []

    async def load_profiles(self) -> list[DeviceProfile]:
        # 1. Read existing saved profiles from vault or disk
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            vdata = self.vault.get_profiles()
            if vdata:
                profiles = [DeviceProfile.from_dict(d) for d in vdata if isinstance(d, dict)]
                if profiles:
                    self._profiles_cache = profiles
                    return profiles
        elif self._profiles_file.is_file():
            try:
                data = json.loads(self._profiles_file.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    profiles = [DeviceProfile.from_dict(d) for d in data if isinstance(d, dict)]
                    if profiles:
                        self._profiles_cache = profiles
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
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            vdata = self.vault.get_profiles()
            if vdata:
                updated = [DeviceProfile.from_dict(d) for d in vdata if isinstance(d, dict)]
            updated = [p for p in updated if p.name != profile.name and p.address != profile.address]
            updated.append(profile)
            self.vault.set_profiles([p.to_dict() for p in updated])
            self._profiles_cache = updated
            return

        if self._profiles_file.is_file():
            try:
                data = json.loads(self._profiles_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    updated = [DeviceProfile.from_dict(d) for d in data if isinstance(d, dict)]
            except Exception:
                pass
        updated = [p for p in updated if p.name != profile.name and p.address != profile.address]
        updated.append(profile)
        self._profiles_cache = updated
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
            if serial is None:
                raise RuntimeError("pyserial is not installed for Serial")
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

            # Determine initial prompt, hostname, vendor directly from opened serial handle (no double-open)
            hostname = extract_hostname(init_resp) or profile.name or port
            prompt = extract_prompt(init_resp) or f"{hostname}>"
            vendor = detect_vendor_from_text(init_resp) if init_resp else profile.vendor
            if vendor == Vendor.AUTO and profile.vendor != Vendor.AUTO:
                vendor = profile.vendor

            # Update profile name
            if not profile.name or profile.name == port:
                profile.name = hostname

            driver = RealSerialSession(session_id, profile, loop, chunk_queue)
            driver.start(ser, prompt)
            self._serial_drivers[session_id] = driver

            # Disable pagination per vendor if enabled
            paging_cmd = self._disable_paging_cmd(vendor)
            if paging_cmd and getattr(self._settings, "disable_paging", True):
                await driver.run_command(paging_cmd)

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
                known_hosts = self._data_dir / "known_hosts"
                if known_hosts.is_file():
                    try:
                        client.load_host_keys(str(known_hosts))
                    except Exception as exc:
                        logger.debug("Failed loading known_hosts: %s", exc)
                else:
                    try:
                        client.load_system_host_keys()
                    except Exception:
                        pass

                policy_name = profile.ssh.host_key_policy
                if policy_name == "strict":
                    client.set_missing_host_key_policy(paramiko.RejectPolicy())
                else:
                    # TOFU / ask: accept new key and persist it to ~/.rackhand/known_hosts
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

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
                if policy_name in ("tofu", "ask"):
                    try:
                        known_hosts.parent.mkdir(parents=True, exist_ok=True)
                        client.save_host_keys(str(known_hosts))
                    except Exception as exc:
                        logger.debug("Failed saving known_hosts: %s", exc)
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

            paging_cmd = self._disable_paging_cmd(vendor)
            if paging_cmd and getattr(self._settings, "disable_paging", True):
                await driver.run_command(paging_cmd)

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

    @staticmethod
    def _disable_paging_cmd(vendor: Vendor) -> Optional[str]:
        if vendor == Vendor.CISCO_ASA:
            return "terminal pager 0"
        if vendor == Vendor.JUNIPER_JUNOS:
            return "set cli screen-length 0"
        if vendor in (Vendor.LINUX, Vendor.VYOS, Vendor.FORTIOS):
            return None
        return "terminal length 0"

    async def disconnect(self, session_id: str) -> None:
        ser_drv = self._serial_drivers.pop(session_id, None)
        if ser_drv:
            ser_drv.close()

        ssh_drv = self._ssh_drivers.pop(session_id, None)
        if ssh_drv:
            ssh_drv.close()

        self._active_sessions.pop(session_id, None)
        q = self._queues.pop(session_id, None)
        if q is not None:
            try:
                q.put_nowait(None)
            except Exception:
                pass

    async def console_stream(self, session_id: str) -> AsyncIterator[ConsoleChunk]:
        q = self._queues.get(session_id)
        if q is None:
            return
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            yield chunk

    async def run_command(self, session_id: str, command: str, source: str = "user") -> CommandResult:
        timeout_s = float(getattr(self._settings, "command_timeout_s", 30) or 30)
        ser_drv = self._serial_drivers.get(session_id)
        if ser_drv:
            return await ser_drv.run_command(command, source=source, timeout_s=timeout_s)

        ssh_drv = self._ssh_drivers.get(session_id)
        if ssh_drv:
            return await ssh_drv.run_command(command, source=source, timeout_s=timeout_s)

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
            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                for k, v in pdata.items():
                    self.vault.set_provider(k, v)
                return
            # Never persist plaintext API keys when an encrypted vault is initialized
            if hasattr(self, "vault") and self.vault and self.vault.is_vault_initialized():
                logger.warning("Skipping plaintext providers.json write while encrypted vault is locked.")
                return
            self._providers_file.write_text(json.dumps(pdata, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed saving provider configs: %s", exc)

    async def load_provider_configs(self) -> dict[str, ProviderConfig]:
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            vdata = self.vault.get_providers()
            for pk, pv in vdata.items():
                self._providers[pk] = ProviderConfig.from_dict(pv)
        return dict(self._providers)

    async def save_provider_config(self, cfg: ProviderConfig) -> None:
        self._providers[cfg.key] = cfg
        self._save_providers_to_disk()

    async def delete_provider_config(self, key: str) -> None:
        self._providers.pop(key, None)
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            try:
                self.vault.delete_provider(key)
                return
            except Exception:
                pass
        self._save_providers_to_disk()

    async def test_provider(self, cfg: ProviderConfig) -> TestResult:
        if cfg.scheme == ApiScheme.ANTIGRAVITY or cfg.spec_id == "antigravity" or cfg.base_url == "local:agy" or "antigravity" in cfg.name.lower():
            start_t = time.time()
            agy_bin = ensure_agy_in_path(getattr(cfg, "api_key", None))
            if not agy_bin:
                return TestResult(False, "AGY CLI executable ('agy') not found on system PATH or install directories", 0, False)
            try:
                cmd = [agy_bin, "--effort", "low", "--output-format", "json", "-p", "Reply with OK"]
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

        if cfg.base_url and (cfg.api_key or cfg.auth is AuthStyle.NONE or "localhost" in cfg.base_url or "127.0.0.1" in cfg.base_url):
            start_t = time.time()
            try:
                import httpx
                from llm_engine import build_llm_request
                endpoint, headers, payload, is_anthropic = build_llm_request(
                    cfg, [{"role": "user", "content": "hi"}], tools=[], max_tokens=5
                )
                verify_arg: Any = cfg.ca_bundle if (cfg.verify_tls and cfg.ca_bundle) else cfg.verify_tls
                async with httpx.AsyncClient(timeout=min(float(cfg.timeout_s or 10.0), 15.0), verify=verify_arg) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
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

    async def generate_session_title(self, provider: ProviderConfig, prompt: str) -> Optional[str]:
        """Generate a concise, professional 2-4 word session title via active AI provider."""
        if not prompt or not prompt.strip():
            return None
        system_prompt = (
            "You are a network engineering assistant. Generate a concise, professional 2 to 4 word session title "
            "summarizing the operator's prompt. Output ONLY the title text, nothing else. "
            "No quotes, no explanation, no trailing punctuation."
        )
        try:
            raw_title = await self._llm_engine.call_llm_stateless(
                provider=provider,
                system_prompt=system_prompt,
                user_prompt=prompt.strip(),
                temperature=0.2,
                max_tokens=20,
            )
            if not raw_title:
                return None
            first_line = raw_title.strip().splitlines()[0].strip()
            clean = re.sub(r'^(?:\*{1,2}|#+)?\s*(?:session\s+)?title\s*:\s*(?:\*{1,2})?\s*', '', first_line, flags=re.IGNORECASE)
            clean = clean.strip('"`\'*#._- ')
            if len(clean) > 36:
                clean = clean[:33].rstrip() + "..."
            return clean if clean else None
        except Exception as exc:
            logger.debug("AI session title generation failed: %s", exc)
            return None

    async def list_models(self, cfg: ProviderConfig | str) -> list[str]:
        if isinstance(cfg, str):
            cfg_obj = self._providers.get(cfg)
            if cfg_obj:
                cfg = cfg_obj
            else:
                cfg = ProviderConfig(key=cfg, spec_id=cfg, base_url="", auth=AuthStyle.BEARER)

        if cfg.scheme == ApiScheme.ANTIGRAVITY or cfg.spec_id == "antigravity" or cfg.base_url == "local:agy" or "antigravity" in cfg.name.lower():
            agy_bin = ensure_agy_in_path(getattr(cfg, "api_key", None))
            if agy_bin:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        agy_bin, "models",
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
                import httpx
                base = cfg.base_url.rstrip("/")
                models_url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
                headers = dict(cfg.headers)
                if cfg.api_key:
                    if cfg.scheme == ApiScheme.ANTHROPIC:
                        headers["x-api-key"] = cfg.api_key
                        headers.setdefault("anthropic-version", "2023-06-01")
                    elif cfg.auth == AuthStyle.X_API_KEY:
                        headers["x-api-key"] = cfg.api_key
                    elif cfg.auth == AuthStyle.API_KEY_HEADER:
                        headers["api-key"] = cfg.api_key
                    elif cfg.auth == AuthStyle.CUSTOM_HEADER:
                        headers[cfg.auth_header_name] = cfg.api_key
                    elif cfg.auth == AuthStyle.BEARER:
                        headers["Authorization"] = f"Bearer {cfg.api_key}"
                verify_arg: Any = cfg.ca_bundle if (cfg.verify_tls and cfg.ca_bundle) else cfg.verify_tls
                async with httpx.AsyncClient(timeout=10.0, verify=verify_arg) as client:
                    resp = await client.get(models_url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        items = data.get("data") or data.get("models") or []
                        models = [m.get("id") or m.get("name") for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]
                        if models:
                            return models
            except Exception as exc:
                logger.debug("Endpoint model listing failed for %s: %s", cfg.key, exc)
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
            if cfg.key in self._providers:
                existing = self._providers[cfg.key]
                if existing.api_key and isinstance(existing.api_key, str) and existing.api_key.startswith("sk-ant-oat"):
                    existing.auth = AuthStyle.BEARER
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
        is_plan_mode: bool = False,
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
            is_plan_mode=is_plan_mode,
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

    def inject_mid_turn_prompt(self, session_id: str, prompt: str) -> bool:
        if hasattr(self, "_llm_engine") and hasattr(self._llm_engine, "inject_mid_turn_prompt"):
            return self._llm_engine.inject_mid_turn_prompt(session_id, prompt)
        return False

    async def compact_session(
        self,
        session_id: str,
        provider: ProviderConfig,
        session: Optional[DeviceSession] = None,
        agent: Optional[Any] = None,
        is_plan_mode: bool = False,
        fallback_transcript: str = "",
    ) -> str:
        return await self._llm_engine.compact_session(
            session_id=session_id,
            provider=provider,
            session=session,
            agent_spec=agent,
            is_plan_mode=is_plan_mode,
            fallback_transcript=fallback_transcript,
        )

    def clear_session_memory(self, session_id: str) -> None:
        self._llm_engine.clear_session(session_id)

    def clone_session_memory(self, source_session_id: str, target_session_id: str) -> None:
        self._llm_engine.clone_session(source_session_id, target_session_id)

    def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self._llm_engine.memory.get_messages(session_id)

    def set_session_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._llm_engine.set_session_messages(session_id, messages)

    # ---- chat session persistence -----------------------------------------------

    async def load_chat_sessions(self) -> tuple[list[ChatSession], Optional[str]]:
        def _load_index_sync() -> tuple[list[ChatSession], Optional[str]]:
            sessions: list[ChatSession] = []
            active_id: Optional[str] = None

            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                raw_sessions = self.vault.get_sessions_index()
                for d in raw_sessions:
                    if isinstance(d, dict) and "id" in d:
                        sessions.append(ChatSession.from_dict(d))
                        ui_m, mem = self.vault.get_session_details(d["id"])
                        if mem:
                            self._llm_engine.memory.set_messages(d["id"], mem)
                active_id = sessions[0].id if sessions else None
                return sessions, active_id

            if self._sessions_index_file.is_file():
                try:
                    data = json.loads(self._sessions_index_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        active_id = data.get("active_session_id")
                        raw_list = data.get("sessions", [])
                        if isinstance(raw_list, list):
                            for item in raw_list:
                                if isinstance(item, dict):
                                    sessions.append(ChatSession.from_dict(item))
                except Exception as exc:
                    logger.debug("Failed to read sessions index: %s", exc)

            # Fallback recovery: if index missing or empty, discover from sessions dir
            if not sessions and self._sessions_dir.is_dir():
                for f in sorted(self._sessions_dir.glob("*.json")):
                    try:
                        sdata = json.loads(f.read_text(encoding="utf-8"))
                        meta = sdata.get("session") or sdata
                        if isinstance(meta, dict) and "id" in meta:
                            sessions.append(ChatSession.from_dict(meta))
                    except Exception:
                        pass
                sessions.sort(key=lambda s: s.num)

            # Pre-seed memory in LLM engine for loaded sessions
            for s in sessions:
                s_file = self._sessions_dir / f"{s.id}.json"
                if s_file.is_file():
                    try:
                        sdata = json.loads(s_file.read_text(encoding="utf-8"))
                        mem = sdata.get("memory", [])
                        if isinstance(mem, list):
                            self._llm_engine.memory.set_messages(s.id, mem)
                    except Exception:
                        pass

            return sessions, active_id

        return await asyncio.to_thread(_load_index_sync)

    async def load_chat_session_details(self, session_id: str) -> tuple[list[ChatMessage], list[dict[str, Any]]]:
        def _load_details_sync() -> tuple[list[ChatMessage], list[dict[str, Any]]]:
            from ui.messages import deserialize_message

            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                raw_ui, mem_msgs = self.vault.get_session_details(session_id)
                ui_msgs = []
                for m in raw_ui:
                    obj = deserialize_message(m)
                    if obj:
                        ui_msgs.append(obj)
                return ui_msgs, (mem_msgs if isinstance(mem_msgs, list) else [])

            s_file = self._sessions_dir / f"{session_id}.json"
            if not s_file.is_file():
                return [], []
            try:
                data = json.loads(s_file.read_text(encoding="utf-8"))
                raw_msgs = data.get("messages", [])
                ui_msgs: list[ChatMessage] = []
                for m in raw_msgs:
                    msg_obj = deserialize_message(m)
                    if msg_obj:
                        ui_msgs.append(msg_obj)
                mem_msgs = data.get("memory", [])
                return ui_msgs, (mem_msgs if isinstance(mem_msgs, list) else [])
            except Exception as exc:
                logger.debug("Failed to load details for session %s: %s", session_id, exc)
                return [], []

        return await asyncio.to_thread(_load_details_sync)

    async def save_chat_session(
        self,
        session: ChatSession,
        ui_messages: list[ChatMessage],
        memory_messages: Optional[list[dict[str, Any]]] = None,
        is_active: bool = False,
    ) -> None:
        def _save_sync() -> None:
            mem = memory_messages if memory_messages is not None else self._llm_engine.memory.get_messages(session.id)
            serialized_msgs = [m.to_dict() for m in ui_messages if hasattr(m, "to_dict")]

            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                self.vault.set_session_details(session.id, session.to_dict(), serialized_msgs, mem)
                return

            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            s_file = self._sessions_dir / f"{session.id}.json"
            tmp_file = self._sessions_dir / f"{session.id}.tmp"

            payload = {
                "session": session.to_dict(),
                "messages": serialized_msgs,
                "memory": mem,
                "updated_at": datetime.now().isoformat(),
            }
            content = json.dumps(payload, indent=2, ensure_ascii=False)
            try:
                tmp_file.write_text(content, encoding="utf-8")
                os.replace(tmp_file, s_file)
            except Exception:
                # Direct write fallback for Windows file locks
                s_file.write_text(content, encoding="utf-8")
                try:
                    if tmp_file.exists():
                        tmp_file.unlink()
                except Exception:
                    pass

        await asyncio.to_thread(_save_sync)

    async def delete_chat_session_persisted(self, session_id: str) -> None:
        def _delete_sync() -> None:
            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                self.vault.delete_session(session_id)
                return

            s_file = self._sessions_dir / f"{session_id}.json"
            if s_file.exists():
                try:
                    s_file.unlink()
                except Exception:
                    pass
            tmp_file = self._sessions_dir / f"{session_id}.tmp"
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

        await asyncio.to_thread(_delete_sync)

    async def save_session_index(self, sessions: list[ChatSession], active_session_id: Optional[str]) -> None:
        def _save_index_sync() -> None:
            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                self.vault.set_sessions_index([s.to_dict() for s in sessions])
                return

            payload = {
                "active_session_id": active_session_id,
                "sessions": [s.to_dict() for s in sessions],
                "updated_at": datetime.now().isoformat(),
            }
            content = json.dumps(payload, indent=2, ensure_ascii=False)
            tmp = self._dir / "sessions.tmp"
            try:
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, self._sessions_index_file)
            except Exception:
                self._sessions_index_file.write_text(content, encoding="utf-8")
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

        await asyncio.to_thread(_save_index_sync)

    async def append_session_transcript(self, session: ChatSession, entry: str, settings: Optional[Settings] = None) -> None:
        if settings is not None and not settings.keep_transcripts:
            return
        def _append_sync() -> None:
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", session.title or f"session_{session.num}").strip()
            if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
                self.vault.append_transcript(session.id, safe_title, entry)
                return

            raw_dir = getattr(settings, "transcript_dir", "~/.rackhand/transcripts") if settings else "~/.rackhand/transcripts"
            tdir = Path(os.path.expanduser(raw_dir))
            tdir.mkdir(parents=True, exist_ok=True)
            tfile = tdir / f"{safe_title}_{session.id}.md"
            with tfile.open("a", encoding="utf-8") as f:
                f.write(entry + "\n\n")

        try:
            await asyncio.to_thread(_append_sync)
        except Exception as exc:
            logger.debug("Failed to append session transcript: %s", exc)

    # ---- settings ---------------------------------------------------------------

    async def load_settings(self) -> Settings:
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            vdata = self.vault.get_settings()
            if isinstance(vdata, dict):
                return Settings.from_dict(vdata)

        if self._settings_file.is_file():
            try:
                data = json.loads(self._settings_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return Settings.from_dict(data)
            except Exception:
                pass
        return Settings()

    async def save_settings(self, settings: Settings) -> None:
        if hasattr(self, "vault") and self.vault and self.vault.is_unlocked():
            self.vault.set_settings(settings.to_dict())
            return

        try:
            self._settings_file.write_text(
                json.dumps(settings.to_dict(), indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass



# =============================================================================
