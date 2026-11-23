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
# 4. DEMO BACKEND (realistic enterprise Cisco / multi-vendor mock)
# =============================================================================

_IOS_IP_INT_BRIEF = """Interface              IP-Address      OK? Method Status                Protocol
Vlan1                  unassigned      YES unset  administratively down down    
Vlan10                 10.10.10.1      YES manual up                    up      
Vlan20                 10.10.20.1      YES manual up                    up      
Vlan30                 10.10.30.1      YES manual up                    up      
Vlan99                 10.10.99.1      YES manual up                    up      
GigabitEthernet1/0/1   unassigned      YES unset  up                    up      
GigabitEthernet1/0/2   unassigned      YES unset  up                    up      
GigabitEthernet1/0/3   unassigned      YES unset  down                  down    
GigabitEthernet1/0/4   unassigned      YES unset  administratively down down    
GigabitEthernet1/0/23  unassigned      YES unset  up                    up      
GigabitEthernet1/0/24  unassigned      YES unset  up                    up      
TenGigabitEthernet1/1/1 unassigned     YES unset  up                    up      
TenGigabitEthernet1/1/2 unassigned     YES unset  down                  down    
Loopback0              10.255.255.1    YES manual up                    up      """

_IOS_INT_STATUS = """Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   ESXI-HOST-01       connected    trunk      a-full a-1000 10/100/1000BaseTX
Gi1/0/2   ESXI-HOST-02       connected    trunk      a-full a-1000 10/100/1000BaseTX
Gi1/0/3   MGMT-PRINTER       notconnect   10           auto   auto 10/100/1000BaseTX
Gi1/0/4   UNASSIGNED         disabled     1            auto   auto 10/100/1000BaseTX
Gi1/0/23  AP-CORRIDOR-01     connected    99         a-full a-1000 10/100/1000BaseTX
Gi1/0/24  UPLINK-TO-DIST01   connected    trunk      a-full a-1000 10/100/1000BaseTX
Te1/1/1   CORE-TRUNK-10G     connected    trunk        full    10G SFP-10G-SR
Te1/1/2   CORE-TRUNK-10G-STBY notconnect  trunk        auto   auto SFP-10G-SR"""

_IOS_VERSION = """Cisco IOS XE Software, Version 17.09.04a
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.4a, RELEASE SOFTWARE (fc2)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2023 by Cisco Systems, Inc.
Compiled Fri 28-Jul-23 03:12 by mcpre

ROM: IOS-XE ROMMON
BOOTLDR: System Bootstrap, Version 17.9.1r, RELEASE SOFTWARE (P)

cat9300-core-01 uptime is 34 weeks, 3 days, 14 hours, 22 minutes
Uptime for this architecture is 34 weeks, 3 days, 14 hours, 24 minutes
System returned to ROM by reload
System image file is "bootflash:packages.conf"
Last reload reason: Reload command

cisco C9300-48UXM (X86) processor with 2949174K/6147K bytes of memory.
Processor board ID FOC24382R5Q
48 Gigabit Ethernet interfaces
4 Ten Gigabit Ethernet interfaces
2 40 Gigabit Ethernet interfaces
2048K bytes of non-volatile configuration memory.
16384K bytes of physical memory.
11468800K bytes of Bootflash at bootflash:.

Base Ethernet MAC Address          : 70:70:8b:24:e1:00
Configuration register is 0x102"""

_IOS_VLAN_BRIEF = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/5, Gi1/0/6, Gi1/0/7
10   DATA_USERS                       active    Gi1/0/8, Gi1/0/9, Gi1/0/10
20   VOIP_PHONES                      active    Gi1/0/11, Gi1/0/12, Gi1/0/13
30   SERVERS_DMZ                      active    
99   INFRA_MGMT                       active    Gi1/0/23
100  ISOLATED_GUEST                   active    Gi1/0/14, Gi1/0/15
1002 fddi-default                     act/unsup 
1003 token-ring-default               act/unsup 
1004 fddinet-default                  act/unsup 
1005 trnet-default                    act/unsup """

_IOS_CDP_NEIGHBORS = """Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge
                  S - Switch, H - Host, I - IGMP, r - Repeater, P - Phone, 
                  D - Remote, C - CVTA, M - Two-port Mac Relay 

Device ID        Local Intrfce     Hldtme    Capability  Platform  Port ID
cat9500-dist-01  Te1/1/1           142              S I  C9500-32Q Ten 1/0/1
edge-rtr01       Gi1/0/24          165            R S I  ISR4451-X GigabitEthernet0/0/0
ap-corridor-01   Gi1/0/23          179              H T  AIR-AP380 GigabitEthernet0
esxi-compute-01  Gi1/0/1           131                H  VMware ES vmnic0
esxi-compute-02  Gi1/0/2           128                H  VMware ES vmnic0"""

_IOS_LLDP_NEIGHBORS = """Chassis id: 7081.053e.10aa
Port id: Gi1/0/1
Port Description: Uplink
System Name: esxi-compute-01.lab
System Description: VMware ESXi 8.0.2 build-22380479

Chassis id: f4bd.9e45.9900
Port id: TenGigabitEthernet1/0/1
Port Description: Core Trunk 10G
System Name: cat9500-dist-01
System Description: Cisco IOS XE Software, Catalyst L3 Switch (CAT9K_IOSXE)"""

_IOS_INVENTORY = """NAME: "c9300 Chassis", DESCR: "Cisco Catalyst 9300 Series Switch"
PID: C9300-48UXM      , VID: V02  , SN: FOC24382R5Q

NAME: "Switch 1 - Power Supply A", DESCR: "1100W AC Power Supply"
PID: PWR-C1-1100WAC   , VID: V01  , SN: LIT24350F2K

NAME: "Switch 1 - Power Supply B", DESCR: "1100W AC Power Supply"
PID: PWR-C1-1100WAC   , VID: V01  , SN: LIT24350F2L

NAME: "Switch 1 - Fan 1", DESCR: "Cisco Catalyst 9300 Fan Module"
PID: FAN-T2           , VID: V01  , SN: NWG243003AL

NAME: "Te1/1/1", DESCR: "SFP-10G-SR"
PID: SFP-10G-SR       , VID: V01  , SN: FNS2419089Q"""

_IOS_ENV_ALL = """Sensor List: Environmental Monitor
 Sensor                  Location        State          Reading
 Temp: Inlet             Switch 1        NORMAL         28 Celsius
 Temp: Outlet            Switch 1        NORMAL         36 Celsius
 Temp: ASIC              Switch 1        NORMAL         45 Celsius
 Fan 1                   Switch 1        NORMAL         4800 RPM
 Fan 2                   Switch 1        NORMAL         4850 RPM
 Fan 3                   Switch 1        NORMAL         4790 RPM
 Power Supply A          Switch 1        NORMAL         1100 Watts (Status: OK)
 Power Supply B          Switch 1        NORMAL         1100 Watts (Status: OK)
 PoE Available: 1440 Watts, PoE Consumed: 185 Watts"""

_IOS_IP_ROUTE = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area 
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2

Gateway of last resort is 10.10.99.254 to network 0.0.0.0

S*    0.0.0.0/0 [1/0] via 10.10.99.254, Vlan99
      10.0.0.0/8 is variably subnetted, 8 subnets, 2 masks
C        10.10.10.0/24 is directly connected, Vlan10
L        10.10.10.1/32 is directly connected, Vlan10
C        10.10.20.0/24 is directly connected, Vlan20
L        10.10.20.1/32 is directly connected, Vlan20
C        10.10.30.0/24 is directly connected, Vlan30
L        10.10.30.1/32 is directly connected, Vlan30
C        10.10.99.0/24 is directly connected, Vlan99
L        10.10.99.1/32 is directly connected, Vlan99
O     10.200.0.0/16 [110/2] via 10.10.99.254, 02:41:18, Vlan99"""

_IOS_RUNNING_CONFIG = """Building configuration...

Current configuration : 3842 bytes
!
version 17.9
service timestamps debug datetime msec
service timestamps log datetime msec
service password-encryption
!
hostname cat9300-core-01
!
boot-start-marker
boot system bootflash:packages.conf
boot-end-marker
!
no logging console
enable secret 9 $9$vD4.f8...dummyhash
!
username netadmin privilege 15 secret 9 $9$kH3.e2...dummyhash
!
aaa new-model
aaa authentication login default local
aaa authorization exec default local
!
vtp mode transparent
!
vlan 10
 name DATA_USERS
!
vlan 20
 name VOIP_PHONES
!
vlan 30
 name SERVERS_DMZ
!
vlan 99
 name INFRA_MGMT
!
spanning-tree mode rapid-pvst
spanning-tree extend system-id
!
interface GigabitEthernet1/0/1
 description ESXI-HOST-01
 switchport mode trunk
 switchport trunk allowed vlan 10,20,30,99
!
interface GigabitEthernet1/0/23
 description AP-CORRIDOR-01
 switchport access vlan 99
 switchport mode access
 power inline auto
!
interface GigabitEthernet1/0/24
 description UPLINK-TO-DIST01
 switchport mode trunk
 switchport trunk allowed vlan 10,20,99
!
interface TenGigabitEthernet1/1/1
 description CORE-TRUNK-10G
 switchport mode trunk
 switchport trunk allowed vlan 10,20,30,99
!
interface Vlan10
 ip address 10.10.10.1 255.255.255.0
!
interface Vlan20
 ip address 10.10.20.1 255.255.255.0
!
interface Vlan30
 ip address 10.10.30.1 255.255.255.0
!
interface Vlan99
 ip address 10.10.99.1 255.255.255.0
!
ip default-gateway 10.10.99.254
ip routing
!
line con 0
 exec-timeout 15 0
 stopbits 1
line vty 0 4
 transport input ssh
 exec-timeout 15 0
!
end"""

_IOS_MAC_TABLE = """          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  10    7081.053e.10aa    DYNAMIC     Gi1/0/8
  10    7081.053e.10ab    DYNAMIC     Gi1/0/9
  20    0004.f211.8901    DYNAMIC     Gi1/0/11
  99    7070.8b24.e100    STATIC      CPU
  99    3820.56a1.c023    DYNAMIC     Gi1/0/23
Total Mac Addresses for this criterion: 5"""

_IOS_LOGGING = """Syslog logging: enabled (0 messages dropped, 3 messages rate-limited)
    Console logging: disabled
    Monitor logging: level debugging, 0 messages logged
    Buffer logging:  level debugging, 184 messages logged

Log Buffer (4096 bytes):
*Nov 19 08:30:12.104: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/24, changed state to up
*Nov 19 08:30:13.105: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/24, changed state to up
*Nov 19 08:31:00.412: %SYS-5-CONFIG_I: Configured from console by netadmin on vty0 (10.10.99.50)
*Nov 19 09:12:44.891: %SEC_LOGIN-5-LOGIN_SUCCESS: Login Success [user: netadmin] [Source: 10.10.99.50]"""

_FORTI_STATUS = """Version: FortiGate-60F v7.4.4,build2662,240314 (GA.M)
Serial-Number: FGT60FTK2209XXXX
Hostname: edge-fw01
Operation Mode: NAT
Current HA mode: standalone
System time: Thu Nov 19 09:21:07 2026"""

_FORTI_IFACES = """== [ wan1 ]  ip: 203.0.113.2 255.255.255.248  status: up
== [ lan ]   ip: 192.168.1.99 255.255.255.0   status: up
== [ dmz ]   ip: 192.168.50.1 255.255.255.0   status: up"""

_LIVE_CATALOG_CACHE: list[dict] = []
_LIVE_CATALOG_TIME: float = 0.0


async def _fetch_openrouter_catalog() -> list[dict]:
    global _LIVE_CATALOG_CACHE, _LIVE_CATALOG_TIME
    import time
    now = time.time()
    if _LIVE_CATALOG_CACHE and (now - _LIVE_CATALOG_TIME < 600):
        return _LIVE_CATALOG_CACHE
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://openrouter.ai/api/v1/models")
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    _LIVE_CATALOG_CACHE = data
                    _LIVE_CATALOG_TIME = now
                    return data
    except Exception:
        pass
    return _LIVE_CATALOG_CACHE


def _extract_models_from_catalog(provider_key: str, catalog: list[dict]) -> list[str]:
    slug_map = {
        "anthropic": ["anthropic/", "~anthropic/"],
        "openai": ["openai/", "~openai/"],
        "gemini": ["google/", "~google/"],
        "vertex": ["google/", "anthropic/"],
        "xai": ["x-ai/", "~x-ai/"],
        "deepseek": ["deepseek/", "~deepseek/"],
        "mistral": ["mistralai/"],
        "groq": ["meta-llama/", "mistralai/", "groq/"],
        "ollama": ["meta-llama/", "mistralai/", "qwen/"],
        "openrouter": [""],
        "cohere": ["cohere/"],
        "qwen": ["qwen/"],
        "amazon": ["amazon/"],
        "bedrock": ["anthropic/", "amazon/", "meta-llama/"],
        "together": ["meta-llama/", "deepseek/", "qwen/"],
        "fireworks": ["accounts/fireworks/models/"],
        "cerebras": ["meta-llama/"],
        "sambanova": ["meta-llama/", "qwen/"],
        "nvidia": ["meta/", "nvidia/", "mistralai/"],
        "perplexity": ["perplexity/"],
        "azure": ["openai/"],
        "moonshot": ["moonshotai/", "~moonshotai/"],
        "zai": ["z-ai/", "~z-ai/"],
    }
    prefixes = slug_map.get(provider_key.lower(), [provider_key.lower() + "/"])
    results: list[str] = []
    seen: set[str] = set()
    for m in catalog:
        mid = m.get("id", "")
        for p in prefixes:
            if not p or mid.startswith(p):
                clean = mid[len(p):] if p else mid
                if ":batch" in clean:
                    continue
                if clean not in seen:
                    seen.add(clean)
                    results.append(clean)
    return results


async def _query_native_endpoint_models(cfg: ProviderConfig) -> list[str]:
    import httpx
    headers = {"User-Agent": "Rackhand/1.0"}
    if cfg.auth is AuthStyle.BEARER and cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    elif cfg.auth is AuthStyle.X_API_KEY and cfg.api_key:
        headers["x-api-key"] = cfg.api_key
        headers["anthropic-version"] = "2023-06-01"
    elif cfg.auth is AuthStyle.API_KEY_HEADER and cfg.api_key:
        headers["api-key"] = cfg.api_key
    elif cfg.auth is AuthStyle.CUSTOM_HEADER and cfg.auth_header_name and cfg.api_key:
        headers[cfg.auth_header_name] = cfg.api_key

    url = cfg.base_url.rstrip("/")
    if cfg.scheme is ApiScheme.OLLAMA:
        query_url = f"{url}/api/tags"
    elif cfg.scheme is ApiScheme.ANTHROPIC:
        query_url = f"{url}/v1/models"
    elif cfg.scheme is ApiScheme.GEMINI:
        query_url = f"{url}/models?key={cfg.api_key}" if cfg.api_key else f"{url}/models"
    else:
        query_url = f"{url}/models"

    async with httpx.AsyncClient(timeout=6.0, verify=cfg.verify_tls) as client:
        resp = await client.get(query_url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data and isinstance(data["data"], list):
                return [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
            elif "models" in data and isinstance(data["models"], list):
                return [
                    m.get("name", "").replace("models/", "")
                    for m in data["models"]
                    if isinstance(m, dict) and "name" in m
                ]
    return []


class DemoBackend(Backend):
    def __init__(self) -> None:
        self._profiles: list[DeviceProfile] = [
            DeviceProfile(
                "cat9300-core-01",
                Transport.SSH,
                Vendor.CISCO_IOS,
                ssh=SSHSettings("10.10.1.1", 22, "netadmin"),
                notes="MDF Rack 04 Unit 12 - Catalyst 9300-48UXM Core Switch",
            ),
            DeviceProfile(
                "cat9500-dist-01",
                Transport.SSH,
                Vendor.CISCO_IOS,
                ssh=SSHSettings("10.10.1.2", 22, "netadmin"),
                notes="MDF Rack 04 Unit 16 - Catalyst 9500-32QC Aggregation Spine",
            ),
            DeviceProfile(
                "isr4451-edge-rtr",
                Transport.SERIAL,
                Vendor.CISCO_IOS,
                serial=SerialSettings("COM3", 9600),
                notes="Rack 01 Unit 02 - ISR 4451-X/K9 Demarc Router",
            ),
            DeviceProfile(
                "nexus9k-spine-01",
                Transport.SSH,
                Vendor.CISCO_NXOS,
                ssh=SSHSettings("10.200.0.1", 22, "admin"),
                notes="DC Fabric Pod 1 Top-of-Rack - Nexus 93180YC-FX3",
            ),
            DeviceProfile(
                "edge-fw01",
                Transport.SSH,
                Vendor.FORTIOS,
                ssh=SSHSettings("192.168.1.99", 22, "admin"),
                notes="Perimeter Firewall - FortiGate-60F v7.4.4",
            ),
            DeviceProfile(
                "c2960x-access-01",
                Transport.SERIAL,
                Vendor.CISCO_IOS,
                serial=SerialSettings("/dev/ttyUSB0", 9600),
                notes="IDF Closet 2B - Catalyst 2960X-48FPS-L",
            ),
        ]
        self._providers: dict[str, ProviderConfig] = {
            "anthropic": ProviderConfig(
                key="anthropic",
                name="Anthropic",
                spec_id="anthropic",
                scheme=ApiScheme.ANTHROPIC,
                base_url="https://api.anthropic.com",
                api_key="sk-ant-api03-mock-token-live",
                auth=AuthStyle.X_API_KEY,
                model="claude-3-5-sonnet-20241022",
                context_window=200000,
                max_output=8192,
                supports_tools=True,
                supports_streaming=True,
            )
        }
        self._settings = Settings()
        self._queues: dict[str, asyncio.Queue] = {}
        self._sessions: dict[str, DeviceSession] = {}
        self._vendors: dict[str, Vendor] = {}
        self._approvals: dict[str, asyncio.Future] = {}

    # ---- discovery ----
    async def list_serial_ports(self) -> list[SerialPortInfo]:
        await asyncio.sleep(0.15)
        return [
            SerialPortInfo("COM3", "Cisco USB Console (VID:PID 05A6:0009)", "USB VID:PID=05A6:0009"),
            SerialPortInfo("/dev/ttyUSB0", "FTDI FT232R USB-Serial Rollover Cable", "USB VID:PID=0403:6001"),
            SerialPortInfo("COM7", "Prolific PL2303 USB-to-Serial Bridge", "USB VID:PID=067B:2303"),
            SerialPortInfo("/dev/ttyACM0", "Opengear CM7100 Console Server Port 1", "USB VID:PID=10C4:EA60"),
        ]

    async def probe_device(
        self,
        port_or_host: str,
        baud: Optional[int] = None,
        transport: Transport = Transport.SERIAL,
    ) -> DeviceProbeResult:
        await asyncio.sleep(0.35)
        p = (port_or_host or "").strip()

        # In rack lab:
        # COM7 or 192.168.1.1 -> FortiGate 60F
        if "COM7" in p or "192.168.1.1" in p or "forti" in p.lower():
            return DeviceProbeResult(
                ok=True,
                port_or_host=p,
                baud=baud or 9600,
                prompt="edge-fw01 # ",
                hostname="edge-fw01",
                vendor=Vendor.FORTIOS,
                message="FortiGate-60F (FortiOS v7.4.4) prompt detected",
            )
        # /dev/ttyACM0 or 10.20.1.1 -> Arista 7050SX
        if "ttyACM0" in p or "10.20.1.1" in p or "spine" in p.lower() or "arista" in p.lower():
            return DeviceProbeResult(
                ok=True,
                port_or_host=p,
                baud=baud or 9600,
                prompt="spine-sw01#",
                hostname="spine-sw01",
                vendor=Vendor.ARISTA_EOS,
                message="Arista 7050SX (EOS) prompt detected",
            )
        # /dev/ttyUSB0 or leaf -> Cisco Nexus 9000
        if "ttyUSB0" in p or "leaf" in p.lower() or "nxos" in p.lower():
            return DeviceProbeResult(
                ok=True,
                port_or_host=p,
                baud=baud or 115200,
                prompt="nxos9k-leaf-01#",
                hostname="nxos9k-leaf-01",
                vendor=Vendor.CISCO_NXOS,
                message="Cisco Nexus 9300 (NX-OS) prompt detected",
            )
        # Default / COM3 / 10.10.1.1 -> Cisco Catalyst 9300
        return DeviceProbeResult(
            ok=True,
            port_or_host=p or "COM3",
            baud=baud or 9600,
            prompt="cat9300-core-01#",
            hostname="cat9300-core-01",
            vendor=Vendor.CISCO_IOS,
            message="Cisco Catalyst 9300 (IOS-XE) prompt detected",
        )

    async def probe_baud(self, port: str) -> Optional[int]:
        res = await self.probe_device(port, transport=Transport.SERIAL)
        return res.baud if res.ok else 9600

    async def load_profiles(self) -> list[DeviceProfile]:
        return list(self._profiles)

    async def save_profile(self, profile: DeviceProfile) -> None:
        self._profiles = [p for p in self._profiles if p.name != profile.name] + [profile]

    # ---- sessions ----
    async def connect(self, profile: DeviceProfile, secrets: dict[str, str]) -> DeviceSession:
        await asyncio.sleep(0.4)
        if profile.ssh and profile.ssh.host.endswith(".99.99"):
            raise ConnectionError("No route to host (demo failure for 192.168.99.99)")

        target_key = ""
        if profile.transport is Transport.SERIAL and profile.serial:
            target_key = profile.serial.port
        elif profile.ssh:
            target_key = profile.ssh.host

        probe = await self.probe_device(
            target_key,
            baud=profile.serial.baud if profile.serial else None,
            transport=profile.transport,
        )

        vendor = profile.vendor if profile.vendor is not Vendor.AUTO else probe.vendor
        forti = vendor is Vendor.FORTIOS

        # Auto-detect hostname if profile name is missing, empty, or generic fallback
        generic_names = {
            target_key, profile.address, "COM3", "COM7", "/dev/ttyUSB0", "/dev/ttyACM0",
            "device", "switch", "router", "e.g. cat9300-core-01",
        }
        is_generic = (
            not profile.name
            or profile.name.strip() in generic_names
            or profile.name.startswith("10.")
            or profile.name.startswith("192.168.")
        )

        detected_host = probe.hostname or ("edge-fw01" if forti else "cat9300-core-01")
        host = profile.name.strip() if (profile.name and not is_generic) else detected_host

        prompt = probe.prompt or (f"{host} # " if forti else f"{host}#")
        sess = DeviceSession(uuid.uuid4().hex[:8], profile, ConnState.CONNECTED, prompt, host,
                             "privileged", vendor)
        self._sessions[sess.id] = sess
        q: asyncio.Queue = asyncio.Queue()
        self._queues[sess.id] = q
        self._vendors[sess.id] = vendor

        # Synchronize profile name with detected hostname
        if not profile.name or is_generic:
            profile.name = host

        if forti:
            banner = "\r\nFortiGate-60F v7.4.4,build2662,240314 (GA.M)\r\nedge-fw01 login: admin\r\n"
        elif vendor is Vendor.CISCO_NXOS:
            banner = (
                "\r\nCisco Nexus Operating System (NX-OS) Software\r\n"
                "TAC support: http://www.cisco.com/tac\r\n"
                f"User Access Verification\r\n\r\n"
                f"Username: {profile.ssh.username if profile.ssh else 'admin'}\r\n"
            )
        elif vendor is Vendor.ARISTA_EOS:
            banner = (
                "\r\nArista Networks EOS version 4.28.3M\r\n"
                f"spine-sw01 login: {profile.ssh.username if profile.ssh else 'admin'}\r\n"
            )
        else:
            banner = (
                "\r\n*******************************************************************************\r\n"
                "* WARNING: Cisco Systems IOS-XE Network Appliance. Authorized Access Only.     *\r\n"
                "* Disconnect IMMEDIATELY if you are not an authorized network administrator.  *\r\n"
                "*******************************************************************************\r\n\r\n"
                "User Access Verification\r\n\r\n"
                f"Username: {profile.ssh.username if profile.ssh else 'netadmin'}\r\n"
                "Password: \r\n"
                f"Last login: Thu Nov 19 09:14:02 2026 from 10.10.99.50\r\n\r\n"
            )

        # Confirm and refine hostname detected directly from the wire banner & prompt
        wire_host = extract_hostname(banner + prompt)
        if wire_host:
            sess.hostname = wire_host
            if is_generic or not profile.name:
                profile.name = wire_host

        q.put_nowait(ConsoleChunk(sess.id, banner + prompt, "device"))
        return sess

    async def disconnect(self, session_id: str) -> None:
        self._queues.pop(session_id, None)
        self._sessions.pop(session_id, None)

    async def console_stream(self, session_id: str) -> AsyncIterator[ConsoleChunk]:
        q = self._queues.get(session_id)
        while q is not None:
            yield await q.get()

    def _canned(self, vendor: Vendor, cmd: str) -> tuple[str, bool]:
        # Support agents sending \r, \n, or \r\n
        c = (cmd or "").replace("\r\n", "\n").replace("\r", "\n").strip().lower()
        if not c or c in ("cr", "\r", "\n", "\r\n", "<cr>"):
            return "", True

        if c.startswith("ping"):
            target = cmd.strip().split()[-1] if len(cmd.strip().split()) > 1 else "10.10.99.254"
            return (
                f"Type escape sequence to abort.\n"
                f"Sending 5, 100-byte ICMP Echos to {target}, timeout is 2 seconds:\n"
                f"!!!!!\n"
                f"Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/4 ms"
            ), True

        if vendor is Vendor.FORTIOS:
            if c.startswith("get system status"):
                return _FORTI_STATUS, True
            if c.startswith(("get system interface", "show system interface")):
                return _FORTI_IFACES, True
            if c.startswith(("get router info", "show router")):
                return (
                    "Codes: K - kernel, C - connected, S - static, R - RIP, B - BGP\n"
                    "       O - OSPF, IA - OSPF inter area\n"
                    "S*      0.0.0.0/0 [10/0] via 203.0.113.1, wan1\n"
                    "C       192.168.1.0/24 is directly connected, lan\n"
                    "C       192.168.50.0/24 is directly connected, dmz\n"
                    "C       203.0.113.0/29 is directly connected, wan1"
                ), True
            if c.startswith(("diag sys top", "diagnose sys top")):
                return (
                    "Run Time:  41 days,  7 hours and 12 minutes\n"
                    "0U, 0N, 1S, 99I, 0WA, 0HI, 0SI, 0ST; 1984T, 722F, 1262U\n"
                    "       newcli      492      R        0.9     1.4\n"
                    "         sshd      480      S        0.0     0.8\n"
                    "      miglogd      168      S        0.0     1.1"
                ), True
            if c.startswith(("show firewall policy", "get firewall policy")):
                return (
                    "config firewall policy\n"
                    "    edit 1\n"
                    "        set name \"LAN_TO_WAN\"\n"
                    "        set srcintf \"lan\"\n"
                    "        set dstintf \"wan1\"\n"
                    "        set action accept\n"
                    "        set schedule \"always\"\n"
                    "        set service \"ALL\"\n"
                    "        set nat enable\n"
                    "    next\n"
                    "end"
                ), True
            return "Unknown action 0", False

        # Cisco IOS-XE / NX-OS commands
        if c.startswith(("terminal length", "term len")):
            return "", True

        if c.startswith(("show ip int", "sh ip int")):
            return _IOS_IP_INT_BRIEF, True

        if c.startswith(("show interfaces status", "sh int status", "sh int stat", "show int status")):
            return _IOS_INT_STATUS, True

        if c.startswith(("show ver", "sh ver")):
            return _IOS_VERSION, True

        if c.startswith(("show running-config interface", "sh run int")):
            parts = cmd.strip().split()
            iface = parts[-1] if len(parts) > 1 else "GigabitEthernet1/0/24"
            return (
                f"Building configuration...\n\n"
                f"Current configuration :\n"
                f"!\n"
                f"interface {iface}\n"
                f" description {iface.upper()}-TRUNK\n"
                f" switchport mode trunk\n"
                f" switchport trunk allowed vlan 10,20,99\n"
                f"end"
            ), True

        if c.startswith(("show run", "sh run", "show running")):
            return _IOS_RUNNING_CONFIG, True

        if c.startswith(("show vlan", "sh vlan")):
            return _IOS_VLAN_BRIEF, True

        if c.startswith(("show cdp nei", "sh cdp nei", "show cdp")):
            return _IOS_CDP_NEIGHBORS, True

        if c.startswith(("show lldp", "sh lldp")):
            return _IOS_LLDP_NEIGHBORS, True

        if c.startswith(("show inv", "sh inv")):
            return _IOS_INVENTORY, True

        if c.startswith(("show env", "sh env")):
            return _IOS_ENV_ALL, True

        if c.startswith(("show ip route", "sh ip route", "sh route", "show route")):
            return _IOS_IP_ROUTE, True

        if c.startswith(("show mac", "sh mac")):
            return _IOS_MAC_TABLE, True

        if c.startswith(("show log", "sh log")):
            return _IOS_LOGGING, True

        if c.startswith(("show proc", "sh proc")):
            return (
                "CPU utilization for five seconds: 4%/0%; one minute: 5%; five minutes: 4%\n"
                " PID Runtime(ms)   Invoked      uSecs   5Sec   1Min   5Min TTY Process\n"
                "   1           0         3          0  0.00%  0.00%  0.00%   0 Chunk Manager\n"
                "   2           0        12          0  0.00%  0.00%  0.00%   0 Load Meter\n"
                " 112      314159    420192        747  0.42%  0.38%  0.35%   0 IOS-XE Net-Ops"
            ), True

        if c.startswith(("show span", "sh span")):
            return (
                "Switch is in rapid-pvst mode\n"
                "Root bridge for: VLAN0010, VLAN0020, VLAN0030, VLAN0099\n"
                "Extended system ID is enabled\n"
                "PortFast BPDU Guard is disabled by default\n"
                "UplinkFast is disabled\n"
                "BackboneFast is disabled"
            ), True

        if c.startswith(("write memory", "wr mem", "wr", "copy run start", "copy running-config startup-config")):
            return "Building configuration...\n[OK]", True

        if c.startswith("hostname "):
            return "", True

        if c.startswith(("conf t", "configure terminal", "interface", "vlan", "ip address", "no shut", "exit", "end")):
            return "", True

        return "% Invalid input detected at '^' marker.", False

    async def run_command(self, session_id: str, command: str, source: str = "user") -> CommandResult:
        await asyncio.sleep(random.uniform(0.08, 0.22))
        q = self._queues.get(session_id)
        sess = self._sessions.get(session_id)
        prompt_str = f"{sess.prompt} " if sess else "# "

        # Support agents sending \r, \n, or \r\n
        norm_cmd = (command or "").replace("\r\n", "\n").replace("\r", "\n")
        cmd_lines = [l.strip() for l in norm_cmd.split("\n") if l.strip()]
        if not cmd_lines:
            if q:
                q.put_nowait(ConsoleChunk(session_id, f"\r\n{prompt_str}", source))
            return CommandResult("", "", 50, True)

        out, ok = self._canned(self._vendors.get(session_id, Vendor.CISCO_IOS), command)

        # Track dynamic hostname changes on the device
        c_low = command.strip().lower()
        if c_low.startswith("hostname "):
            parts = command.strip().split()
            if len(parts) > 1 and parts[1]:
                new_h = parts[1]
                if sess:
                    sess.hostname = new_h
                    sess.profile.name = new_h
                    sess.prompt = f"{new_h}#"
                    prompt_str = f"{new_h}# "

        if q:
            sep = "\r\n" if out and not out.endswith("\n") else ""
            q.put_nowait(ConsoleChunk(session_id, f"{command}\r\n{out}{sep}\r\n{prompt_str}", source))
        return CommandResult(out, "", 140, ok)

    async def send_control(self, session_id: str, action: str) -> None:
        q = self._queues.get(session_id)
        sess = self._sessions.get(session_id)
        p_str = f"{sess.prompt} " if sess and action != "break" else ""
        labels = {
            "cr": f"\r\n{p_str}",
            "ctrl_c": f"^C\r\n{p_str}",
            "ctrl_z": f"^Z\r\n{p_str}",
            "break": "\r\n*** BREAK sent ***\r\nrommon 1 > ",
        }
        if q:
            q.put_nowait(ConsoleChunk(session_id, labels.get(action, ""), "user"))

    # ---- providers ----
    async def load_provider_configs(self) -> dict[str, ProviderConfig]:
        return dict(self._providers)

    async def save_provider_config(self, cfg: ProviderConfig) -> None:
        self._providers[cfg.key] = cfg

    async def delete_provider_config(self, key: str) -> None:
        self._providers.pop(key, None)

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
            if cfg.key not in self._providers or not self._providers[cfg.key].api_key or "mock" in str(self._providers[cfg.key].api_key):
                self._providers[cfg.key] = cfg
                imported.append(cfg)
        return imported

    async def test_provider(self, cfg: ProviderConfig) -> TestResult:
        await asyncio.sleep(0.5)
        if cfg.auth is not AuthStyle.NONE and not cfg.api_key and "localhost" not in cfg.base_url:
            return TestResult(False, "401 Unauthorized: missing API key", 120)
        if not cfg.base_url.startswith(("http://", "https://")):
            return TestResult(False, "Base URL must start with http:// or https://", 0)
        return TestResult(True, "Reachable, authenticated", 142, cfg.supports_tools)

    async def list_models(self, cfg: ProviderConfig | str) -> list[str]:
        if isinstance(cfg, str):
            cfg_obj = self._providers.get(cfg)
            if cfg_obj:
                cfg = cfg_obj
            else:
                from models import AuthStyle
                cfg = ProviderConfig(key=cfg, spec_id=cfg, base_url="", auth=AuthStyle.BEARER)
        # 1. Query the live native endpoint if configured with an API key or local server
        if cfg.base_url and (cfg.api_key or "localhost" in cfg.base_url or "127.0.0.1" in cfg.base_url):
            try:
                models = await _query_native_endpoint_models(cfg)
                if models:
                    return models
            except Exception:
                pass

        # 2. Dynamically fetch latest models from the live catalog
        prov_key = cfg.spec_id or cfg.key
        try:
            catalog = await _fetch_openrouter_catalog()
            if catalog:
                models = _extract_models_from_catalog(prov_key, catalog)
                if models:
                    return models
        except Exception:
            pass

        # 3. Fallback to curated models
        from models import PROVIDER_MODELS
        if prov_key in PROVIDER_MODELS:
            return [m[1] for m in PROVIDER_MODELS[prov_key]]

        return [f"{cfg.spec_id or 'custom'}-large", f"{cfg.spec_id or 'custom'}-medium",
                f"{cfg.spec_id or 'custom'}-small"]

    # ---- agent ----
    async def run_turn(self, prompt, session, provider, settings, agent=None, chat_session_id=None):
        forti = session.vendor is Vendor.FORTIOS
        p_low = prompt.lower()

        async def say(text: str):
            for word in text.split(" "):
                await asyncio.sleep(0.015)
                yield AgentText(word + " ")

        if "audit" in p_low or "secur" in p_low or "harden" in p_low:
            async for ev in say(f"Starting security and configuration audit on **{session.hostname}** (IOS-XE Enterprise Baseline)..."):
                yield ev
            cmd = "show running-config"
            yield AgentToolCall("audit-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("audit-1", res.output[:750] + "\n... [remaining configuration verified]", res.ok)

            async for ev in say("\n\n**Audit Analysis Results**:\n"
                                "1. `GigabitEthernet1/0/24` trunk is missing VLAN 30 (SERVERS_DMZ isolated from distribution).\n"
                                "2. `line vty 0 4` lacks IP access restriction (`access-class 10 in` missing).\n"
                                "3. VTY session timeout should be locked to 15 minutes (`exec-timeout 15 0`).\n\n"
                                "Here is the recommended configuration remediation:"):
                yield ev

            cmds = [
                "interface GigabitEthernet1/0/24",
                " description UPLINK-TO-DIST01",
                " switchport trunk allowed vlan add 30",
                "line vty 0 4",
                " access-class 10 in",
                " exec-timeout 15 0",
                " transport input ssh",
            ]
            before = ("interface GigabitEthernet1/0/24\n switchport trunk allowed vlan 10,20,99\n"
                      "line vty 0 4\n transport input ssh")
            after = ("interface GigabitEthernet1/0/24\n description UPLINK-TO-DIST01\n switchport trunk allowed vlan 10,20,30,99\n"
                     "line vty 0 4\n access-class 10 in\n exec-timeout 15 0\n transport input ssh")
            rollback = [
                "interface GigabitEthernet1/0/24",
                " switchport trunk allowed vlan remove 30",
                "line vty 0 4",
                " no access-class 10 in",
            ]
            change = ProposedChange(
                uuid.uuid4().hex[:8], session.hostname,
                "Harden VTY lines & prune Gi1/0/24 trunk",
                cmds, Risk.CONFIG,
                "Restricts SSH management access to authorized subnets and extends VLAN 30 across the trunk.",
                rollback, before, after
            )
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._approvals[change.id] = fut
            yield AgentProposal(change)
            decision: ApprovalDecision = await fut
            if not decision.approved:
                async for ev in say("\n\nRemediation rejected by operator. Device running-config left unchanged."):
                    yield ev
            else:
                for idx, c in enumerate(decision.commands):
                    c_clean = c.strip()
                    if not c_clean:
                        continue
                    c_id = f"apply-{idx + 1}"
                    yield AgentToolCall(c_id, session.hostname, c_clean, Risk.CONFIG)
                    res = await self.run_command(session.id, c_clean, "agent")
                    yield AgentToolResult(c_id, res.output, res.ok)

                if decision.save_after:
                    yield AgentToolCall("save-mem", session.hostname, "write memory", Risk.CONFIG)
                    res = await self.run_command(session.id, "write memory", "agent")
                    yield AgentToolResult("save-mem", "[OK] Configuration saved to NVRAM.", True)

                tail = " Configuration written to NVRAM (write memory)." if decision.save_after else " Running-config modified in RAM only."
                async for ev in say(f"\n\n✓ **Remediation applied successfully** ({len(decision.commands)} commands executed on {session.hostname})." + tail):
                    yield ev

        elif "iface" in p_low or "interface" in p_low or "port" in p_low or "health" in p_low:
            async for ev in say(f"Polling physical and logical interface telemetry on **{session.hostname}**..."):
                yield ev
            cmd = "show interfaces status"
            yield AgentToolCall("if-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("if-1", res.output, res.ok)

            cmd2 = "show ip interface brief"
            yield AgentToolCall("if-2", session.hostname, cmd2, Risk.READ)
            res2 = await self.run_command(session.id, cmd2, "agent")
            yield AgentToolResult("if-2", res2.output, res2.ok)

            async for ev in say("\n\n**Interface Telemetry Summary**:\n"
                                "- **Core Uplinks**: `Te1/1/1` (10G SFP-10G-SR) and `Gi1/0/24` (1000BaseTX) are active (`connected/trunk`).\n"
                                "- **Access Ports**: `Gi1/0/23` powering AP-CORRIDOR-01 via PoE (inline auto, 15.4W allocated).\n"
                                "- **Notices**: `Gi1/0/4` is administratively disabled (`down/down`). `Te1/1/2` standby uplink is `notconnect`.\n"
                                "- Error counters: 0 CRC, 0 runts, 0 frame errors across all active PHYs."):
                yield ev

        elif "backup" in p_low or "snapshot" in p_low or "save" in p_low:
            async for ev in say(f"Generating running-config backup snapshot on **{session.hostname}**..."):
                yield ev
            cmd = "copy running-config flash:/backups/cat9300-core-01-snapshot.cfg"
            yield AgentToolCall("bk-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("bk-1", "[OK] Copied 3,842 bytes to flash:/backups/cat9300-core-01-snapshot.cfg", True)

            async for ev in say("\n\n✓ Snapshot created: `flash:/backups/cat9300-core-01-snapshot.cfg` (3,842 bytes). Archive verified."):
                yield ev

        elif "cdp" in p_low or "lldp" in p_low or "neighbor" in p_low or "topolog" in p_low:
            async for ev in say(f"Scanning CDP / LLDP neighbor tables on **{session.hostname}**..."):
                yield ev
            cmd = "show cdp neighbors"
            yield AgentToolCall("cdp-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("cdp-1", res.output, res.ok)

            async for ev in say("\n\n**Discovered Rack Topology**:\n"
                                "```\n"
                                " [cat9500-dist-01]  (Spine Switch Ten1/0/1)\n"
                                "        │\n"
                                "     (Te1/1/1 - 10G Trunk)\n"
                                "        │\n"
                                " [cat9300-core-01]  <-- ACTIVE NODE\n"
                                "   ├── Gi1/0/24  ───>  [edge-rtr01] (ISR 4451-X WAN Demarc)\n"
                                "   ├── Gi1/0/1   ───>  [esxi-compute-01] (VMware ESXi Host)\n"
                                "   └── Gi1/0/23  ───>  [ap-corridor-01] (AIR-AP3800 PoE)\n"
                                "```"):
                yield ev

        elif "vlan" in p_low or "trunk" in p_low:
            async for ev in say(f"Inspecting 802.1Q trunking and VLAN database on **{session.hostname}**..."):
                yield ev
            cmd = "show vlan brief"
            yield AgentToolCall("vlan-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("vlan-1", res.output, res.ok)

            async for ev in say("\n\n**VLAN Allocation Summary**:\n"
                                "- `VLAN 10` (DATA_USERS): Gi1/0/8-10, routed on SVI 10.10.10.1.\n"
                                "- `VLAN 20` (VOIP_PHONES): Gi1/0/11-13, DSCP QoS trusted.\n"
                                "- `VLAN 30` (SERVERS_DMZ): Isolated server segment, SVI 10.10.30.1.\n"
                                "- `VLAN 99` (INFRA_MGMT): Out-of-band management subnet 10.10.99.0/24.\n"
                                "- `VLAN 100` (ISOLATED_GUEST): Guest wireless subnet."):
                yield ev

        else:
            async for ev in say(f"Received instruction: *\"{prompt}\"* for **{session.hostname}**.\n"
                                f"Checking device baseline before proceeding..."):
                yield ev
            cmd = "show version" if not forti else "get system status"
            yield AgentToolCall("gen-1", session.hostname, cmd, Risk.READ)
            res = await self.run_command(session.id, cmd, "agent")
            yield AgentToolResult("gen-1", res.output[:500] + "\n...", res.ok)
            async for ev in say(f"\n\n**{session.hostname}** is operating nominally on {session.vendor.value.upper()}. "
                                "You can click any of the Diagnostic Macros on the left or type specific CLI queries."):
                yield ev

        yield AgentDone(1940, 380)

    async def resolve_approval(self, change_id: str, decision: ApprovalDecision) -> None:
        fut = self._approvals.pop(change_id, None)
        if fut and not fut.done():
            fut.set_result(decision)

    async def cancel_turn(self) -> None:
        for fut in self._approvals.values():
            if not fut.done():
                fut.set_result(ApprovalDecision(False))
        self._approvals.clear()

    async def load_settings(self) -> Settings:
        return self._settings

    async def save_settings(self, settings: Settings) -> None:
        self._settings = settings


# =============================================================================
# 5. REAL HARDWARE & SSH BACKEND
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
            if cfg.key not in self._providers or not self._providers[cfg.key].api_key or "mock" in str(self._providers[cfg.key].api_key):
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
