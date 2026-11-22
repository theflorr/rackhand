from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Optional, Union

# =============================================================================
# 1. HARDWARE & DEVICE ENUMS / MODELS
# =============================================================================

class Transport(str, Enum):
    SERIAL = "serial"
    SSH = "ssh"


class Vendor(str, Enum):
    AUTO = "auto"
    CISCO_IOS = "cisco_ios"
    CISCO_NXOS = "cisco_nxos"
    CISCO_ASA = "cisco_asa"
    FORTIOS = "fortios"
    JUNIPER_JUNOS = "juniper_junos"
    ARISTA_EOS = "arista_eos"
    VYOS = "vyos"
    LINUX = "linux"
    GENERIC = "generic"


VENDOR_LABELS = {
    Vendor.AUTO: "Auto-detect",
    Vendor.CISCO_IOS: "Cisco IOS / IOS-XE",
    Vendor.CISCO_NXOS: "Cisco NX-OS",
    Vendor.CISCO_ASA: "Cisco ASA / FTD",
    Vendor.FORTIOS: "Fortinet FortiOS",
    Vendor.JUNIPER_JUNOS: "Juniper Junos",
    Vendor.ARISTA_EOS: "Arista EOS",
    Vendor.VYOS: "VyOS",
    Vendor.LINUX: "Linux / Cumulus / SONiC",
    Vendor.GENERIC: "Generic CLI",
}


class Risk(str, Enum):
    READ = "read"        # show/get/diagnose - safe to auto-run
    CONFIG = "config"    # changes running config
    DANGER = "danger"    # reload, erase, format, factory-reset, ACL/route on mgmt path...


RISK_LABEL = {
    Risk.READ: "READ-ONLY",
    Risk.CONFIG: "CHANGES CONFIG",
    Risk.DANGER: "DANGEROUS",
}


class ConnState(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class SerialPortInfo:
    device: str            # "COM3" or "/dev/ttyUSB0"
    description: str = ""
    hwid: str = ""


@dataclass
class SerialSettings:
    port: str = ""
    baud: int = 9600
    data_bits: int = 8
    parity: str = "N"          # N/E/O
    stop_bits: str = "1"       # 1 / 1.5 / 2
    flow: str = "none"         # none / rtscts / xonxoff
    line_ending: str = "CR"    # CR / CRLF / LF


@dataclass
class SSHSettings:
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = "password"            # password / key / agent
    key_path: str = ""
    jump_host: str = ""               # optional bastion, user@host:port
    host_key_policy: str = "ask"      # ask / tofu / strict


@dataclass
class DeviceProfile:
    name: str
    transport: Transport
    vendor: Vendor = Vendor.AUTO
    serial: Optional[SerialSettings] = None
    ssh: Optional[SSHSettings] = None
    uses_enable: bool = True
    notes: str = ""

    @property
    def address(self) -> str:
        if self.transport is Transport.SERIAL and self.serial:
            return f"{self.serial.port} @ {self.serial.baud}"
        if self.ssh:
            return f"{self.ssh.username + '@' if self.ssh.username else ''}{self.ssh.host}:{self.ssh.port}"
        return "-"

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "transport": self.transport.value,
            "vendor": self.vendor.value,
            "uses_enable": self.uses_enable,
            "notes": self.notes,
        }
        if self.serial:
            d["serial"] = {
                "port": self.serial.port,
                "baud": self.serial.baud,
                "data_bits": self.serial.data_bits,
                "parity": self.serial.parity,
                "stop_bits": self.serial.stop_bits,
                "flow": self.serial.flow,
                "line_ending": self.serial.line_ending,
            }
        if self.ssh:
            d["ssh"] = {
                "host": self.ssh.host,
                "port": self.ssh.port,
                "username": self.ssh.username,
                "auth": self.ssh.auth,
                "key_path": self.ssh.key_path,
                "jump_host": self.ssh.jump_host,
                "host_key_policy": self.ssh.host_key_policy,
            }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DeviceProfile:
        ser = None
        if "serial" in d and d["serial"]:
            sd = d["serial"]
            ser = SerialSettings(
                port=sd.get("port", ""),
                baud=sd.get("baud", 9600),
                data_bits=sd.get("data_bits", 8),
                parity=sd.get("parity", "N"),
                stop_bits=sd.get("stop_bits", "1"),
                flow=sd.get("flow", "none"),
                line_ending=sd.get("line_ending", "CR"),
            )
        ssh = None
        if "ssh" in d and d["ssh"]:
            shd = d["ssh"]
            ssh = SSHSettings(
                host=shd.get("host", ""),
                port=shd.get("port", 22),
                username=shd.get("username", ""),
                auth=shd.get("auth", "password"),
                key_path=shd.get("key_path", ""),
                jump_host=shd.get("jump_host", ""),
                host_key_policy=shd.get("host_key_policy", "ask"),
            )
        vendor_val = d.get("vendor", "auto")
        try:
            vendor = Vendor(vendor_val)
        except Exception:
            vendor = Vendor.AUTO
        transport_val = d.get("transport", "serial")
        try:
            transport = Transport(transport_val)
        except Exception:
            transport = Transport.SERIAL

        return cls(
            name=d.get("name", "device"),
            transport=transport,
            vendor=vendor,
            serial=ser,
            ssh=ssh,
            uses_enable=d.get("uses_enable", True),
            notes=d.get("notes", ""),
        )


@dataclass
class DeviceSession:
    id: str
    profile: DeviceProfile
    state: ConnState = ConnState.CONNECTED
    prompt: str = ""
    hostname: str = ""
    mode: str = "exec"                # exec / privileged / config / rommon
    vendor: Vendor = Vendor.GENERIC   # resolved after auto-detect


@dataclass
class DeviceProbeResult:
    ok: bool
    port_or_host: str
    baud: int = 9600
    prompt: str = ""
    hostname: str = ""
    vendor: Vendor = Vendor.AUTO
    message: str = ""


# Hostname / prompt regex patterns matching real networking gear (Cisco, FortiOS, Junos, Arista, etc.)
_RE_PROMPT_HOSTS = [
    re.compile(r"(?:^|[\r\n])\s*([A-Za-z0-9_\-\.]+)(?:\([^\)]+\))?\s*[>#]\s*$", re.MULTILINE),
    re.compile(r"(?:^|[\r\n])\s*([A-Za-z0-9_\-\.]+)(?:\s+\([^\)]+\))?\s+[#\$]\s*$", re.MULTILINE),
    re.compile(r"(?:^|[\r\n])\s*[\w\-\.]+@([A-Za-z0-9_\-\.]+)(?::\S+)?\s*[>#\$]\s*$", re.MULTILINE),
    re.compile(r"\[[\w\-\.]+@([A-Za-z0-9_\-\.]+)\]\s*[>#\$]", re.MULTILINE),
    re.compile(r"(?:^|[\r\n])\s*[<\[]([A-Za-z0-9_\-\.]+)[>\]]\s*$", re.MULTILINE),
    re.compile(r"(?im)^\s*(?:hostname|sysname|host-name)\s+([A-Za-z0-9_\-\.]+)\b"),
    re.compile(r"(?im)^\s*([A-Za-z0-9_\-\.]+)\s+uptime\s+is\b"),
    re.compile(r"(?im)^\s*([A-Za-z0-9_\-\.]+)\s+login:\s*"),
]

_IGNORE_HOSTNAMES = {
    "login", "password", "username", "user", "admin", "building", "rommon",
    "connection", "connected", "type", "enter", "press", "warning", "cisco",
    "authorized", "console", "terminal", "line", "end", "exit", "quit",
}


def extract_hostname(text: str) -> Optional[str]:
    """Extract network device hostname from prompt, banner, or command output."""
    if not text:
        return None
    for pattern in _RE_PROMPT_HOSTS:
        matches = pattern.findall(text)
        if matches:
            for match in reversed(matches):
                cand = match.strip() if isinstance(match, str) else match[0].strip()
                if cand and cand.lower() not in _IGNORE_HOSTNAMES and len(cand) <= 63:
                    if any(c.isalnum() for c in cand):
                        return cand
    return None


def extract_prompt(text: str) -> Optional[str]:
    """Extract the active CLI prompt from the tail of the output."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        last = lines[-1]
        if any(last.endswith(c) for c in (">", "#", "$", ":")):
            return last
    return None


def detect_vendor_from_text(text: str) -> Vendor:
    """Detect vendor platform from device banner, version output, or prompt format."""
    lower = text.lower()
    if any(k in lower for k in ("ios-xe", "ios xe", "catalyst", "c9300", "c9200", "c9500", "cisco ios", "cisco systems")):
        return Vendor.CISCO_IOS
    if any(k in lower for k in ("nx-os", "nexus")):
        return Vendor.CISCO_NXOS
    if any(k in lower for k in ("fortigate", "fortios", "fortinet")):
        return Vendor.FORTIOS
    if any(k in lower for k in ("junos", "juniper", "srx", "qfx", "ex4300", "ex2300")):
        return Vendor.JUNIPER_JUNOS
    if any(k in lower for k in ("arista", "eos")):
        return Vendor.ARISTA_EOS
    if any(k in lower for k in ("vyos", "edgeos")):
        return Vendor.VYOS
    if any(k in lower for k in ("linux", "ubuntu", "debian", "cumulus", "opengear")):
        return Vendor.LINUX
    return Vendor.GENERIC


@dataclass
class CommandResult:
    output: str
    prompt: str = ""
    duration_ms: int = 0
    ok: bool = True


@dataclass
class ConsoleChunk:
    session_id: str
    text: str
    source: str = "device"            # device / user / agent


@dataclass
class ChatSession:
    id: str
    num: int
    title: Optional[str] = None
    has_prompt: bool = False
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return f"Session {self.num}#"


@dataclass
class TestResult:
    ok: bool
    message: str
    latency_ms: int = 0
    tools_ok: Optional[bool] = None


# =============================================================================
# 2. AGENT STREAM & PROPOSALS
# =============================================================================

@dataclass
class ProposedChange:
    id: str
    device: str
    title: str
    commands: list[str]
    risk: Risk
    rationale: str = ""
    rollback: list[str] = field(default_factory=list)
    before: str = ""
    after: str = ""


@dataclass
class ApprovalDecision:
    approved: bool
    commands: list[str] = field(default_factory=list)   # possibly edited by the user
    save_after: bool = False                            # write mem / execute backup
    note: str = ""


@dataclass
class AgentText:
    delta: str


@dataclass
class AgentToolCall:
    call_id: str
    device: str
    command: str
    risk: Risk = Risk.READ


@dataclass
class AgentToolResult:
    call_id: str
    output: str
    ok: bool = True


@dataclass
class AgentProposal:
    change: ProposedChange


@dataclass
class AgentDone:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class AgentError:
    message: str


AgentEvent = Union[AgentText, AgentToolCall, AgentToolResult, AgentProposal, AgentDone, AgentError]


# =============================================================================
# 3. SETTINGS
# =============================================================================

@dataclass
class Settings:
    approval_mode: str = "ask_changes"   # ask_all / ask_changes / auto_safe (dangerous always asks)
    redact_secrets: bool = True          # mask passwords/PSKs/SNMP before they reach the LLM
    backup_before_change: bool = True
    keep_transcripts: bool = True
    transcript_dir: str = "~/.rackhand/transcripts"
    max_agent_steps: int = 25
    command_timeout_s: int = 30
    disable_paging: bool = True          # terminal length 0 / set output standard
    theme: str = "rackhand"

    def to_dict(self) -> dict:
        return {
            "approval_mode": self.approval_mode,
            "redact_secrets": self.redact_secrets,
            "backup_before_change": self.backup_before_change,
            "keep_transcripts": self.keep_transcripts,
            "transcript_dir": self.transcript_dir,
            "max_agent_steps": self.max_agent_steps,
            "command_timeout_s": self.command_timeout_s,
            "disable_paging": self.disable_paging,
            "theme": self.theme,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Settings:
        return cls(
            approval_mode=d.get("approval_mode", "ask_changes"),
            redact_secrets=d.get("redact_secrets", True),
            backup_before_change=d.get("backup_before_change", True),
            keep_transcripts=d.get("keep_transcripts", True),
            transcript_dir=d.get("transcript_dir", "~/.rackhand/transcripts"),
            max_agent_steps=d.get("max_agent_steps", 25),
            command_timeout_s=d.get("command_timeout_s", 30),
            disable_paging=d.get("disable_paging", True),
            theme=d.get("theme", "rackhand"),
        )


APPROVAL_LABELS = {
    "ask_all": "ask: every command",
    "ask_changes": "ask: changes",
    "auto_safe": "ask: dangerous only",
}


# =============================================================================
# 4. PROVIDER CATALOG & CONFIG
# =============================================================================

class ApiScheme(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"
    GEMINI = "gemini"
    ANTIGRAVITY = "antigravity"
    OLLAMA = "ollama"
    COHERE = "cohere"
    AZURE = "azure-openai"
    BEDROCK = "bedrock"
    VERTEX = "vertex"


SCHEME_LABELS = {
    ApiScheme.ANTHROPIC: "Anthropic Messages",
    ApiScheme.OPENAI_CHAT: "OpenAI-compatible (chat/completions)",
    ApiScheme.OPENAI_RESPONSES: "OpenAI Responses",
    ApiScheme.GEMINI: "Google Gemini (generateContent)",
    ApiScheme.ANTIGRAVITY: "Google Antigravity (AGY CLI)",
    ApiScheme.OLLAMA: "Ollama native",
    ApiScheme.COHERE: "Cohere v2 chat",
    ApiScheme.AZURE: "Azure OpenAI",
    ApiScheme.BEDROCK: "AWS Bedrock Converse",
    ApiScheme.VERTEX: "Google Vertex AI",
}


class AuthStyle(str, Enum):
    BEARER = "bearer"               # Authorization: Bearer <key>
    X_API_KEY = "x-api-key"         # x-api-key: <key>
    API_KEY_HEADER = "api-key"      # api-key: <key>  (Azure style)
    CUSTOM_HEADER = "custom"        # <header name>: <key>
    QUERY = "query"                 # ?key=<key>
    AWS_SIGV4 = "sigv4"             # handled by backend from extras
    NONE = "none"


AUTH_LABELS = {
    AuthStyle.BEARER: "Authorization: Bearer",
    AuthStyle.X_API_KEY: "x-api-key header",
    AuthStyle.API_KEY_HEADER: "api-key header",
    AuthStyle.CUSTOM_HEADER: "Custom header name",
    AuthStyle.QUERY: "Query parameter",
    AuthStyle.AWS_SIGV4: "AWS SigV4 (from extras)",
    AuthStyle.NONE: "No auth",
}


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    group: str
    scheme: ApiScheme
    base_url: str
    auth: AuthStyle = AuthStyle.BEARER
    key_env: str = ""
    tools: bool = True
    streaming: bool = True
    vision: bool = False
    extras_hint: str = ""
    note: str = ""


_G1, _G2, _G3, _G4, _G5 = (
    "AI APIs",
    "Cloud Providers",
    "Gateways",
    "Local / Self-hosted",
    "Enterprise Cloud",
)
_OA = ApiScheme.OPENAI_CHAT

PROVIDERS: list[ProviderSpec] = [
    ProviderSpec("anthropic", "Anthropic", _G1, ApiScheme.ANTHROPIC, "https://api.anthropic.com",
                 AuthStyle.X_API_KEY, "ANTHROPIC_API_KEY", vision=True),
    ProviderSpec("openai", "OpenAI", _G1, ApiScheme.OPENAI_RESPONSES, "https://api.openai.com/v1",
                 AuthStyle.BEARER, "OPENAI_API_KEY", vision=True,
                 note="Switch scheme to chat/completions if you prefer the older API."),
    ProviderSpec("gemini", "Google Gemini", _G1, ApiScheme.GEMINI,
                 "https://generativelanguage.googleapis.com/v1beta", AuthStyle.QUERY, "GEMINI_API_KEY", vision=True),
    ProviderSpec("antigravity", "Google Antigravity", _G1, ApiScheme.ANTIGRAVITY, "local:agy",
                 AuthStyle.NONE, note="Native local Google Antigravity (Gemini 3.8 / Claude Sonnet 4.6)."),
    ProviderSpec("xai", "xAI", _G1, _OA, "https://api.x.ai/v1", key_env="XAI_API_KEY"),
    ProviderSpec("mistral", "Mistral", _G1, _OA, "https://api.mistral.ai/v1", key_env="MISTRAL_API_KEY"),
    ProviderSpec("deepseek", "DeepSeek", _G1, _OA, "https://api.deepseek.com", key_env="DEEPSEEK_API_KEY"),
    ProviderSpec("cohere", "Cohere", _G1, ApiScheme.COHERE, "https://api.cohere.com/v2", key_env="COHERE_API_KEY"),
    ProviderSpec("moonshot", "Moonshot (Kimi)", _G1, _OA, "https://api.moonshot.ai/v1", key_env="MOONSHOT_API_KEY"),
    ProviderSpec("zai", "Z.ai (GLM)", _G1, _OA, "https://api.z.ai/api/paas/v4", key_env="ZAI_API_KEY"),
    ProviderSpec("qwen", "Alibaba Qwen (DashScope)", _G1, _OA,
                 "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", key_env="DASHSCOPE_API_KEY"),
    ProviderSpec("groq", "Groq", _G2, _OA, "https://api.groq.com/openai/v1", key_env="GROQ_API_KEY"),
    ProviderSpec("together", "Together AI", _G2, _OA, "https://api.together.xyz/v1", key_env="TOGETHER_API_KEY"),
    ProviderSpec("fireworks", "Fireworks AI", _G2, _OA, "https://api.fireworks.ai/inference/v1",
                 key_env="FIREWORKS_API_KEY"),
    ProviderSpec("cerebras", "Cerebras", _G2, _OA, "https://api.cerebras.ai/v1", key_env="CEREBRAS_API_KEY"),
    ProviderSpec("sambanova", "SambaNova", _G2, _OA, "https://api.sambanova.ai/v1", key_env="SAMBANOVA_API_KEY"),
    ProviderSpec("nvidia", "NVIDIA NIM", _G2, _OA, "https://integrate.api.nvidia.com/v1", key_env="NVIDIA_API_KEY"),
    ProviderSpec("perplexity", "Perplexity", _G2, _OA, "https://api.perplexity.ai", key_env="PERPLEXITY_API_KEY",
                 tools=False, note="No tool calling: the agent can chat but cannot run device commands."),
    ProviderSpec("openrouter", "OpenRouter", _G3, _OA, "https://openrouter.ai/api/v1", key_env="OPENROUTER_API_KEY",
                 note="One key, hundreds of models."),
    ProviderSpec("litellm", "LiteLLM proxy", _G3, _OA, "http://localhost:4000", key_env="LITELLM_API_KEY"),
    ProviderSpec("ollama", "Ollama", _G4, ApiScheme.OLLAMA, "http://localhost:11434", AuthStyle.NONE,
                 note="Local models. Pick one with tool-calling support."),
    ProviderSpec("lmstudio", "LM Studio", _G4, _OA, "http://localhost:1234/v1", AuthStyle.NONE),
    ProviderSpec("vllm", "vLLM", _G4, _OA, "http://localhost:8000/v1", AuthStyle.NONE),
    ProviderSpec("llamacpp", "llama.cpp server", _G4, _OA, "http://localhost:8080/v1", AuthStyle.NONE),
    ProviderSpec("azure", "Azure OpenAI", _G5, ApiScheme.AZURE, "https://YOUR-RESOURCE.openai.azure.com",
                 AuthStyle.API_KEY_HEADER, "AZURE_OPENAI_API_KEY",
                 extras_hint="api_version=2025-01-01-preview\ndeployment=my-deployment"),
    ProviderSpec("bedrock", "AWS Bedrock", _G5, ApiScheme.BEDROCK,
                 "https://bedrock-runtime.us-east-1.amazonaws.com", AuthStyle.AWS_SIGV4,
                 extras_hint="region=us-east-1\naccess_key_id=\nsecret_access_key=\nsession_token="),
    ProviderSpec("vertex", "Google Vertex AI", _G5, ApiScheme.VERTEX,
                 "https://us-central1-aiplatform.googleapis.com", AuthStyle.BEARER,
                 extras_hint="project=my-project\nlocation=us-central1"),
]
PROVIDER_INDEX = {p.id: p for p in PROVIDERS}

PROVIDER_MODELS: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("Claude Sonnet 5.5 (Recommended)", "claude-sonnet-5.5"),
        ("Claude Haiku 5.5 (Fast)", "claude-haiku-5.5"),
        ("Claude Opus 5.5", "claude-opus-5.5"),
        ("Claude Fable 5.2", "claude-fable-5.2"),
        ("Claude 3.7 Sonnet", "claude-3-7-sonnet"),
        ("Claude 3.5 Sonnet", "claude-3-5-sonnet-20241022"),
    ],
    "openai": [
        ("GPT-6.2 Astra (Recommended)", "gpt-6.2-astra"),
        ("GPT-6.2 Sol (Fast)", "gpt-6.2-sol"),
        ("GPT-6.1 Terra Pro", "gpt-6.1-terra-pro"),
        ("GPT-5.3 Omni", "gpt-5.3-omni"),
        ("o3-mini", "o3-mini"),
        ("GPT-4o", "gpt-4o"),
        ("GPT-4o mini", "gpt-4o-mini"),
    ],
    "gemini": [
        ("Gemini 4 Neon (Recommended)", "gemini-4-neon"),
        ("Gemini 4 Hydrogen (Fast)", "gemini-4-hydrogen"),
        ("Gemini 4 Carbon", "gemini-4-carbon"),
        ("Gemini Omni 1.2 Flash", "gemini-omni-1.2-flash"),
        ("Gemini 2.5 Pro", "gemini-2.5-pro"),
        ("Gemini 1.5 Pro", "gemini-1.5-pro"),
    ],
    "antigravity": [
        ("Gemini 3.8 Flash (High)", "gemini-3.8-flash-high"),
        ("Gemini 3.8 Flash (Medium)", "gemini-3.8-flash-medium"),
        ("Gemini 3.8 Flash (Low)", "gemini-3.8-flash-low"),
        ("Claude Sonnet 4.6 (Thinking)", "claude-sonnet-4-6"),
        ("Claude Opus 4.6 (Thinking)", "claude-opus-4-6-thinking"),
        ("GPT-OSS 120B (Medium)", "gpt-oss-120b-medium"),
    ],
    "deepseek": [
        ("DeepSeek V4.1 Flash (Recommended)", "deepseek-v4.1-flash"),
        ("DeepSeek V4 Pro", "deepseek-v4-pro"),
        ("DeepSeek Pro Latest", "deepseek-pro-latest"),
        ("DeepSeek Chat (V3)", "deepseek-chat"),
        ("DeepSeek Reasoner (R1)", "deepseek-reasoner"),
    ],
    "xai": [
        ("Grok 4.7 (Recommended)", "grok-4.7"),
        ("Grok 4.6", "grok-4.6"),
        ("Grok 4.5", "grok-4.5"),
        ("Grok 2", "grok-2-1212"),
    ],
    "mistral": [
        ("Mistral Large 4 (Recommended)", "mistral-large-4-0"),
        ("Pixtral Large 2502", "pixtral-large-2502"),
        ("Devstral 2 (Code)", "devstral-2512"),
        ("Mistral Medium 3.5", "mistral-medium-3-5"),
        ("Codestral", "codestral-latest"),
    ],
    "groq": [
        ("Llama 4 Maverick (Recommended)", "llama-4-maverick"),
        ("Llama 4 Scout", "llama-4-scout"),
        ("Llama 3.3 70B Versatile", "llama-3.3-70b-versatile"),
        ("Mixtral 8x7B", "mixtral-8x7b-32768"),
    ],
    "ollama": [
        ("Llama 4 (Latest)", "llama4:latest"),
        ("Llama 3.3 (70B)", "llama3.3:latest"),
        ("DeepSeek R1", "deepseek-r1:latest"),
        ("Qwen 2.5 Coder", "qwen2.5-coder:latest"),
    ],
    "openrouter": [
        ("Claude 3.5 Sonnet", "anthropic/claude-3.5-sonnet"),
        ("GPT-4o", "openai/gpt-4o"),
        ("DeepSeek V3", "deepseek/deepseek-chat"),
        ("DeepSeek R1", "deepseek/deepseek-r1"),
        ("Llama 3.3 70B", "meta-llama/llama-3.3-70b-instruct"),
        ("Gemini Flash 1.5", "google/gemini-flash-1.5"),
    ],
    "cohere": [
        ("Command R+ (08-2024)", "command-r-plus-08-2024"),
        ("Command R+", "command-r-plus"),
        ("Command R", "command-r"),
    ],
    "together": [
        ("Llama 3.3 70B Turbo", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        ("Llama 3.1 8B Turbo", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"),
        ("DeepSeek V3", "deepseek-ai/DeepSeek-V3"),
        ("Qwen 2.5 72B Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"),
    ],
    "fireworks": [
        ("Llama 3.3 70B", "accounts/fireworks/models/llama-v3p3-70b-instruct"),
        ("Llama 3.1 70B", "accounts/fireworks/models/llama-v3p1-70b-instruct"),
        ("DeepSeek V3", "accounts/fireworks/models/deepseek-v3"),
        ("Qwen 2.5 72B", "accounts/fireworks/models/qwen2p5-72b-instruct"),
    ],
    "cerebras": [
        ("Llama 3.3 70B", "llama3.3-70b"),
        ("Llama 3.1 70B", "llama3.1-70b"),
        ("Llama 3.1 8B", "llama3.1-8b"),
    ],
    "sambanova": [
        ("Llama 3.3 70B", "Meta-Llama-3.3-70B-Instruct"),
        ("Llama 3.1 8B", "Meta-Llama-3.1-8B-Instruct"),
        ("Qwen 2.5 72B", "Qwen2.5-72B-Instruct"),
    ],
    "nvidia": [
        ("Llama 3.3 70B Instruct", "meta/llama-3.3-70b-instruct"),
        ("Llama 3.1 Nemotron 70B", "nvidia/llama-3.1-nemotron-70b-instruct"),
        ("Mistral Large 2", "mistralai/mistral-large-2-instruct"),
    ],
    "perplexity": [
        ("Sonar Pro", "sonar-pro"),
        ("Sonar", "sonar"),
        ("Sonar Reasoning", "sonar-reasoning"),
    ],
    "azure": [
        ("GPT-4o", "gpt-4o"),
        ("GPT-4o mini", "gpt-4o-mini"),
        ("GPT-4 Turbo", "gpt-4-turbo"),
    ],
    "bedrock": [
        ("Claude 3.5 Sonnet v2", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
        ("Claude 3.5 Haiku", "anthropic.claude-3-5-haiku-20241022-v1:0"),
        ("Llama 3.3 70B", "meta.llama3-3-70b-instruct-v1:0"),
        ("Amazon Nova Pro", "amazon.nova-pro-v1:0"),
    ],
    "vertex": [
        ("Gemini 1.5 Pro", "gemini-1.5-pro"),
        ("Gemini 1.5 Flash", "gemini-1.5-flash"),
        ("Claude 3.5 Sonnet", "claude-3-5-sonnet@20241022"),
    ],
    "qwen": [
        ("Qwen Plus (Recommended)", "qwen-plus"),
        ("Qwen Max", "qwen-max"),
        ("Qwen Turbo", "qwen-turbo"),
        ("Qwen 2.5 Coder 32B", "qwen2.5-coder-32b-instruct"),
    ],
    "moonshot": [
        ("Moonshot v1 8K", "moonshot-v1-8k"),
        ("Moonshot v1 32K", "moonshot-v1-32k"),
        ("Moonshot v1 128K", "moonshot-v1-128k"),
    ],
    "zai": [
        ("GLM-4 Plus", "glm-4-plus"),
        ("GLM-4", "glm-4"),
        ("GLM-4 Flash", "glm-4-flash"),
    ],
    "litellm": [
        ("Claude 3.5 Sonnet", "claude-3-5-sonnet"),
        ("GPT-4o", "gpt-4o"),
        ("Gemini 1.5 Pro", "gemini-1.5-pro"),
        ("Llama 3.3 70B", "llama-3.3-70b"),
    ],
    "lmstudio": [
        ("Default Model", "default"),
        ("Local Model", "local-model"),
    ],
    "vllm": [
        ("Default Model", "default"),
        ("Local Model", "local-model"),
    ],
    "llamacpp": [
        ("Default Model", "default"),
        ("Local Model", "local-model"),
    ],
}


@dataclass
class ProviderConfig:
    """A user's saved configuration of one provider (built-in or custom endpoint)."""
    key: str                                  # spec id, or "custom:<slug>"
    name: str = ""
    spec_id: Optional[str] = None             # None -> custom endpoint
    scheme: ApiScheme = ApiScheme.OPENAI_CHAT
    base_url: str = ""
    api_key: str = ""                         # backend should store in OS keyring, not on disk
    auth: AuthStyle = AuthStyle.BEARER
    auth_header_name: str = ""
    model: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    extras: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    ca_bundle: str = ""
    timeout_s: int = 120
    context_window: int = 0
    max_output: int = 0
    temperature: float = 0.2
    supports_tools: bool = True
    supports_streaming: bool = True

    @property
    def is_custom(self) -> bool:
        return self.spec_id is None

    @property
    def label(self) -> str:
        return f"{self.name or self.key}" + (f" / {self.model}" if self.model else "")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "spec_id": self.spec_id,
            "scheme": self.scheme.value if hasattr(self.scheme, "value") else str(self.scheme),
            "base_url": self.base_url,
            "api_key": self.api_key,
            "auth": self.auth.value if hasattr(self.auth, "value") else str(self.auth),
            "auth_header_name": self.auth_header_name,
            "model": self.model,
            "headers": self.headers,
            "extras": self.extras,
            "verify_tls": self.verify_tls,
            "ca_bundle": self.ca_bundle,
            "timeout_s": self.timeout_s,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "temperature": self.temperature,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ProviderConfig:
        scheme_val = d.get("scheme", ApiScheme.OPENAI_CHAT.value)
        try:
            scheme = ApiScheme(scheme_val)
        except ValueError:
            scheme = ApiScheme.OPENAI_CHAT

        auth_val = d.get("auth", AuthStyle.BEARER.value)
        try:
            auth = AuthStyle(auth_val)
        except ValueError:
            auth = AuthStyle.BEARER

        return cls(
            key=d.get("key", ""),
            name=d.get("name", ""),
            spec_id=d.get("spec_id"),
            scheme=scheme,
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            auth=auth,
            auth_header_name=d.get("auth_header_name", ""),
            model=d.get("model", ""),
            headers=d.get("headers", {}) or {},
            extras=d.get("extras", {}) or {},
            verify_tls=d.get("verify_tls", True),
            ca_bundle=d.get("ca_bundle", ""),
            timeout_s=d.get("timeout_s", 120),
            context_window=d.get("context_window", 0),
            max_output=d.get("max_output", 0),
            temperature=d.get("temperature", 0.2),
            supports_tools=d.get("supports_tools", True),
            supports_streaming=d.get("supports_streaming", True),
        )

