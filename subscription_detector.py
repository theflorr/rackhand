from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from models import ApiScheme, AuthStyle, ProviderConfig


@dataclass
class DetectedSubscription:
    id: str
    name: str
    service: str
    source_label: str
    account_info: str
    spec_id: str
    scheme: ApiScheme
    base_url: str
    api_key: str
    auth: AuthStyle
    recommended_model: str
    auth_header_name: str = ""
    extra_models: list[str] = field(default_factory=list)
    raw_info: dict[str, Any] = field(default_factory=dict)

    def to_provider_config(self) -> ProviderConfig:
        return ProviderConfig(
            key=self.spec_id,
            name=self.name,
            spec_id=self.spec_id,
            scheme=self.scheme,
            base_url=self.base_url,
            api_key=self.api_key,
            auth=self.auth,
            auth_header_name=self.auth_header_name,
            model=self.recommended_model,
            max_output=2048,
            supports_tools=True,
            supports_streaming=True,
        )


def _read_win_credential(target_name: str) -> tuple[Optional[str], Optional[bytes]]:
    """Read a credential from Windows Credential Manager via Advapi32 CredReadW."""
    if os.name != "nt":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR),
            ]

        Advapi32 = ctypes.windll.Advapi32
        CredReadW = Advapi32.CredReadW
        CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        CredReadW.restype = wintypes.BOOL

        pcred = ctypes.POINTER(CREDENTIAL)()
        if CredReadW(target_name, 1, 0, ctypes.byref(pcred)):
            cred = pcred.contents
            blob = bytes((cred.CredentialBlob[i] for i in range(cred.CredentialBlobSize)))
            user = cred.UserName
            Advapi32.CredFree(pcred)
            return user, blob
    except Exception:
        pass
    return None, None


def _get_candidate_dirs() -> list[Path]:
    """Return list of candidate user directories across Windows, Linux, and macOS."""
    dirs: list[Path] = []
    try:
        home = Path.home()
        dirs.append(home)
        dirs.append(home / ".config")
        dirs.append(home / ".local" / "share")
        dirs.append(home / ".local" / "state")
    except Exception:
        pass

    # Windows environment variables
    for env_var in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEPATH"):
        val = os.environ.get(env_var)
        if val:
            try:
                p = Path(val)
                if p not in dirs:
                    dirs.append(p)
            except Exception:
                pass

    return dirs


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def _find_agy_executable() -> Optional[str]:
    import shutil
    found = shutil.which("agy") or shutil.which("antigravity")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
        if candidate.is_file():
            return str(candidate)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Antigravity (Google / DeepMind / AGY CLI / Antigravity IDE)
# ─────────────────────────────────────────────────────────────────────────────
def detect_antigravity() -> Optional[DetectedSubscription]:
    agy_bin = _find_agy_executable()
    user, blob = _read_win_credential("gemini:antigravity")

    # If agy binary or gemini:antigravity credential is found, Antigravity is active
    if agy_bin or blob:
        email = ""
        if blob:
            try:
                d = json.loads(blob.decode("utf-8", errors="replace"))
                email = d.get("id_token_email") or user or ""
            except Exception:
                pass
        account_str = f"{email} (Google Antigravity / AGY)" if email else "Google Antigravity (AGY CLI Active)"

        # Read configured model from settings.json if present
        rec_model = "gemini-3.8-flash-low"
        for base in _get_candidate_dirs():
            cfg = _read_json_safe(base / ".gemini" / "antigravity-cli" / "settings.json")
            if cfg and cfg.get("model"):
                saved_model = str(cfg["model"]).lower()
                if "sonnet" in saved_model:
                    rec_model = "claude-sonnet-4-6"
                elif "opus" in saved_model:
                    rec_model = "claude-opus-4-6-thinking"
                elif "flash" in saved_model:
                    rec_model = "gemini-3.8-flash-low"
                break

        return DetectedSubscription(
            id="detected:antigravity",
            name="Google Antigravity",
            service="Antigravity",
            source_label="Google Antigravity (AGY CLI / Cloud Code)",
            account_info=account_str,
            spec_id="antigravity",
            scheme=ApiScheme.ANTIGRAVITY,
            base_url="local:agy",
            api_key=agy_bin or "agy",
            auth=AuthStyle.NONE,
            recommended_model=rec_model,
            extra_models=[
                "gemini-3.8-flash-low",
                "gemini-3.8-flash-medium",
                "gemini-3.8-flash-high",
                "claude-sonnet-4-6",
                "claude-opus-4-6-thinking",
                "gpt-oss-120b-medium",
            ],
            raw_info={"agy_bin": agy_bin, "email": email},
        )

    # Fallback to API Key environment variables for Google AI Studio
    env_key = os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        return DetectedSubscription(
            id="detected:antigravity",
            name="Google Gemini (AI Studio)",
            service="Gemini",
            source_label="Environment Variable ($GEMINI_API_KEY)",
            account_info="API Key Authentication",
            spec_id="gemini",
            scheme=ApiScheme.GEMINI,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key=env_key,
            auth=AuthStyle.X_API_KEY,
            recommended_model="gemini-2.5-flash",
            extra_models=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-4-carbon", "gemini-4-neon"],
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Claude (Claude Code / Anthropic Pro/Team/Max)
# ─────────────────────────────────────────────────────────────────────────────
def detect_claude() -> Optional[DetectedSubscription]:
    # Check env variables first
    env_token = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if env_token:
        is_oauth = "oauth" in env_token.lower() or env_token.startswith("ey")
        return DetectedSubscription(
            id="detected:claude",
            name="Claude (Anthropic)",
            service="Claude",
            source_label="Environment ($ANTHROPIC_API_KEY)",
            account_info="Environment Credentials",
            spec_id="anthropic",
            scheme=ApiScheme.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key=env_token,
            auth=AuthStyle.BEARER if is_oauth else AuthStyle.X_API_KEY,
            recommended_model="claude-3-5-sonnet-20241022",
            extra_models=["claude-3-5-sonnet-20241022", "claude-sonnet-5.5", "claude-haiku-5.5"],
        )

    # Check Windows Credential Manager
    user, blob = _read_win_credential("Claude Code")
    if not blob:
        user, blob = _read_win_credential("claude-code")
    if blob:
        token = blob.decode("utf-8", errors="replace").strip()
        if token:
            return DetectedSubscription(
                id="detected:claude",
                name="Claude (Anthropic)",
                service="Claude",
                source_label="Windows Credential Manager",
                account_info=f"User {user or 'OAuth'}",
                spec_id="anthropic",
                scheme=ApiScheme.ANTHROPIC,
                base_url="https://api.anthropic.com",
                api_key=token,
                auth=AuthStyle.BEARER,
                recommended_model="claude-3-5-sonnet-20241022",
                extra_models=["claude-3-5-sonnet-20241022", "claude-sonnet-5.5", "claude-haiku-5.5"],
            )

    # Check Claude Code ~/.claude.json and .credentials.json
    candidate_paths: list[Path] = []
    for base in _get_candidate_dirs():
        candidate_paths.append(base / ".claude.json")
        candidate_paths.append(base / ".claude" / ".credentials.json")
        candidate_paths.append(base / ".claude" / "settings.json")
        candidate_paths.append(base / "Claude" / "credentials.json")

    token = ""
    email = ""
    plan = ""
    found_path = None

    for p in candidate_paths:
        data = _read_json_safe(p)
        if not data:
            continue

        oauth_account = data.get("oauthAccount")
        if isinstance(oauth_account, dict):
            token = token or oauth_account.get("token") or oauth_account.get("accessToken") or oauth_account.get("oauthToken") or ""
            email = email or oauth_account.get("emailAddress") or oauth_account.get("email") or ""
            plan = plan or oauth_account.get("billingType") or oauth_account.get("plan") or ""

        claude_oauth = data.get("claudeAiOauth")
        if isinstance(claude_oauth, dict):
            token = token or claude_oauth.get("accessToken") or ""
            plan = plan or claude_oauth.get("subscriptionType") or ""

        if not token:
            token = data.get("primaryApiKey") or data.get("apiKey") or data.get("oauthToken") or ""

        if token or email or plan:
            found_path = p
            if token:
                break

    if token or email or plan:
        account_str = f"{email} ({plan})" if email and plan else (email or plan or "Claude Code Active")
        api_token = token or "claude-code-session-token"
        source = f"Claude Code ({found_path.name if found_path else '.claude.json'})"
        return DetectedSubscription(
            id="detected:claude",
            name="Claude (Anthropic)",
            service="Claude",
            source_label=source,
            account_info=account_str,
            spec_id="anthropic",
            scheme=ApiScheme.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key=api_token,
            auth=AuthStyle.BEARER if api_token.startswith("ey") or "oauth" in str(plan).lower() else AuthStyle.X_API_KEY,
            recommended_model="claude-3-5-sonnet-20241022",
            extra_models=["claude-3-5-sonnet-20241022", "claude-sonnet-5.5", "claude-haiku-5.5"],
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. OpenCode (auth.json / opencode.json)
# ─────────────────────────────────────────────────────────────────────────────
def detect_opencode() -> list[DetectedSubscription]:
    out: list[DetectedSubscription] = []
    seen: set[str] = set()
    candidate_paths: list[Path] = []
    for base in _get_candidate_dirs():
        candidate_paths.append(base / ".local" / "share" / "opencode" / "auth.json")
        candidate_paths.append(base / "opencode" / "auth.json")
        candidate_paths.append(base / ".config" / "opencode" / "opencode.json")
        candidate_paths.append(base / ".config" / "opencode" / "opencode.jsonc")
        candidate_paths.append(base / "AppData" / "Roaming" / "opencode" / "auth.json")
        candidate_paths.append(base / "AppData" / "Local" / "opencode" / "auth.json")

    for p in candidate_paths:
        data = _read_json_safe(p)
        if not data:
            continue

        # 1. Parse auth.json format: {"google": {"type": "oauth", "access": "..."}, "openrouter": {"key": "..."}, ...}
        if "google" in data or "openrouter" in data or "opencode-go" in data or any(isinstance(v, dict) and any(k in v for k in ("key", "access", "apiKey")) for v in data.values()):
            for prov_id, prov_val in data.items():
                if not isinstance(prov_val, dict):
                    continue
                key = (
                    prov_val.get("key")
                    or prov_val.get("access")
                    or prov_val.get("accessToken")
                    or prov_val.get("apiKey")
                    or ""
                )
                if not key or not isinstance(key, str) or key.startswith("{env:"):
                    continue

                clean_id = prov_id.lower().strip()
                sub_id = f"detected:opencode:{clean_id}"
                if sub_id in seen:
                    continue
                seen.add(sub_id)

                if "google" in clean_id or "gemini" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name="Google Gemini via OpenCode",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="Google OAuth Linked",
                        spec_id="gemini",
                        scheme=ApiScheme.GEMINI,
                        base_url="https://generativelanguage.googleapis.com/v1beta",
                        api_key=key,
                        auth=AuthStyle.BEARER,
                        recommended_model="gemini-2.5-flash",
                        extra_models=["gemini-2.5-flash", "gemini-2.5-pro"],
                    ))
                elif "openrouter" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name="OpenRouter via OpenCode",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="OpenRouter API Key",
                        spec_id="openrouter",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://openrouter.ai/api/v1",
                        api_key=key,
                        auth=AuthStyle.BEARER,
                        recommended_model="anthropic/claude-sonnet-5.5",
                        extra_models=["anthropic/claude-sonnet-5.5", "openai/gpt-4o", "deepseek/deepseek-chat"],
                    ))
                elif "anthropic" in clean_id or "claude" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name=f"Claude via OpenCode ({clean_id})",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="Linked via opencode auth",
                        spec_id="anthropic",
                        scheme=ApiScheme.ANTHROPIC,
                        base_url="https://api.anthropic.com",
                        api_key=key,
                        auth=AuthStyle.X_API_KEY,
                        recommended_model="claude-sonnet-5.5",
                    ))
                elif "deepseek" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name="DeepSeek via OpenCode",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="DeepSeek API Key",
                        spec_id="deepseek",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://api.deepseek.com/v1",
                        api_key=key,
                        auth=AuthStyle.BEARER,
                        recommended_model="deepseek-chat",
                    ))
                elif "openai" in clean_id or "gpt" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name=f"OpenAI via OpenCode ({clean_id})",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="Linked via opencode auth",
                        spec_id="openai",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://api.openai.com/v1",
                        api_key=key,
                        auth=AuthStyle.BEARER,
                        recommended_model="gpt-4o",
                    ))
                elif "opencode-go" in clean_id:
                    out.append(DetectedSubscription(
                        id=sub_id,
                        name="OpenCode Go Subscription",
                        service="OpenCode",
                        source_label=f"OpenCode ({p.name})",
                        account_info="OpenCode Go Cloud Key",
                        spec_id="openrouter",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://api.opencode.ai/v1",
                        api_key=key,
                        auth=AuthStyle.BEARER,
                        recommended_model="opencode-go",
                    ))

        # 2. Parse opencode.json format: {"provider": {"nvidia": {"options": {"apiKey": "..."}}, ...}}
        prov_dict = data.get("provider", {}) if isinstance(data.get("provider"), dict) else {}
        for prov_id, prov_cfg in prov_dict.items():
            if not isinstance(prov_cfg, dict):
                continue
            opts = prov_cfg.get("options", {}) if isinstance(prov_cfg.get("options"), dict) else {}
            key = opts.get("apiKey") or prov_cfg.get("apiKey") or ""
            if not key or not isinstance(key, str) or key.startswith("{env:"):
                continue

            clean_id = prov_id.lower().strip()
            sub_id = f"detected:opencode:{clean_id}"
            if sub_id in seen:
                continue
            seen.add(sub_id)

            base_url = opts.get("baseURL") or ""
            prov_name = prov_cfg.get("name") or prov_id.title()

            if "nvidia" in clean_id:
                out.append(DetectedSubscription(
                    id=sub_id,
                    name="NVIDIA NIM via OpenCode",
                    service="OpenCode",
                    source_label=f"OpenCode ({p.name})",
                    account_info="NVIDIA API Key",
                    spec_id="nvidia",
                    scheme=ApiScheme.OPENAI_CHAT,
                    base_url=base_url or "https://integrate.api.nvidia.com/v1",
                    api_key=key,
                    auth=AuthStyle.BEARER,
                    recommended_model="meta/llama-3.3-70b-instruct",
                ))
            elif "deepseek" in clean_id:
                out.append(DetectedSubscription(
                    id=sub_id,
                    name="DeepSeek via OpenCode",
                    service="OpenCode",
                    source_label=f"OpenCode ({p.name})",
                    account_info="DeepSeek API Key",
                    spec_id="deepseek",
                    scheme=ApiScheme.OPENAI_CHAT,
                    base_url=base_url or "https://api.deepseek.com/v1",
                    api_key=key,
                    auth=AuthStyle.BEARER,
                    recommended_model="deepseek-chat",
                ))
            elif "openrouter" in clean_id:
                out.append(DetectedSubscription(
                    id=sub_id,
                    name="OpenRouter via OpenCode",
                    service="OpenCode",
                    source_label=f"OpenCode ({p.name})",
                    account_info="OpenRouter API Key",
                    spec_id="openrouter",
                    scheme=ApiScheme.OPENAI_CHAT,
                    base_url=base_url or "https://openrouter.ai/api/v1",
                    api_key=key,
                    auth=AuthStyle.BEARER,
                    recommended_model="anthropic/claude-sonnet-5.5",
                ))
            else:
                out.append(DetectedSubscription(
                    id=sub_id,
                    name=f"{prov_name} via OpenCode",
                    service="OpenCode",
                    source_label=f"OpenCode ({p.name})",
                    account_info=f"{prov_name} Configuration",
                    spec_id=clean_id,
                    scheme=ApiScheme.OPENAI_CHAT,
                    base_url=base_url or "https://api.openai.com/v1",
                    api_key=key,
                    auth=AuthStyle.BEARER,
                    recommended_model="default",
                ))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 4. GitHub Copilot
# ─────────────────────────────────────────────────────────────────────────────
def detect_github_copilot() -> Optional[DetectedSubscription]:
    # Check env
    env_token = os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env_token:
        return DetectedSubscription(
            id="detected:copilot",
            name="GitHub Copilot",
            service="Copilot",
            source_label="Environment ($COPILOT_GITHUB_TOKEN)",
            account_info="GitHub Token",
            spec_id="copilot",
            scheme=ApiScheme.OPENAI_CHAT,
            base_url="https://api.githubcopilot.com",
            api_key=env_token,
            auth=AuthStyle.BEARER,
            recommended_model="claude-sonnet-5.5",
            extra_models=["claude-sonnet-5.5", "gpt-4o", "o3-mini"],
        )

    # Check Windows Credential Manager
    for target in ("https://github.com/", "git:https://github.com", "GitHub for Visual Studio - https://github.com/"):
        user, blob = _read_win_credential(target)
        if blob:
            token = blob.decode("utf-8", errors="replace").strip()
            if token.startswith(("gho_", "ghp_", "github_pat_")):
                return DetectedSubscription(
                    id="detected:copilot",
                    name="GitHub Copilot",
                    service="Copilot",
                    source_label="Windows Credential Manager (GitHub)",
                    account_info=f"User @{user or 'GitHub'}",
                    spec_id="copilot",
                    scheme=ApiScheme.OPENAI_CHAT,
                    base_url="https://api.githubcopilot.com",
                    api_key=token,
                    auth=AuthStyle.BEARER,
                    recommended_model="claude-sonnet-5.5",
                    extra_models=["claude-sonnet-5.5", "gpt-4o", "o3-mini"],
                )

    # Check config paths: %APPDATA%\GitHub CLI\hosts.yml, %LOCALAPPDATA%\github-copilot\apps.json
    candidate_paths: list[Path] = []
    for base in _get_candidate_dirs():
        candidate_paths.append(base / "GitHub CLI" / "hosts.yml")
        candidate_paths.append(base / ".config" / "gh" / "hosts.yml")
        candidate_paths.append(base / "github-copilot" / "apps.json")
        candidate_paths.append(base / ".copilot" / "config.json")

    for p in candidate_paths:
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            # Parse apps.json style: {"...": {"oauth_token": "..."}}
            if p.suffix == ".json":
                data = json.loads(content)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict) and "oauth_token" in v:
                            return DetectedSubscription(
                                id="detected:copilot",
                                name="GitHub Copilot",
                                service="Copilot",
                                source_label=f"Copilot Client ({p.name})",
                                account_info=f"User {v.get('user', 'OAuth')}",
                                spec_id="copilot",
                                scheme=ApiScheme.OPENAI_CHAT,
                                base_url="https://api.githubcopilot.com",
                                api_key=v["oauth_token"],
                                auth=AuthStyle.BEARER,
                                recommended_model="claude-sonnet-5.5",
                                extra_models=["claude-sonnet-5.5", "gpt-4o", "o3-mini"],
                            )
            # Parse hosts.yml style
            elif "oauth_token:" in content:
                m_token = re.search(r"oauth_token:\s*([^\s]+)", content)
                m_user = re.search(r"user:\s*([^\s]+)", content)
                if m_token:
                    user_str = m_user.group(1) if m_user else "Copilot CLI"
                    return DetectedSubscription(
                        id="detected:copilot",
                        name="GitHub Copilot",
                        service="Copilot",
                        source_label=f"GitHub CLI ({p.name})",
                        account_info=f"User @{user_str}",
                        spec_id="copilot",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://api.githubcopilot.com",
                        api_key=m_token.group(1),
                        auth=AuthStyle.BEARER,
                        recommended_model="claude-sonnet-5.5",
                        extra_models=["claude-sonnet-5.5", "gpt-4o", "o3-mini"],
                    )
        except Exception:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. OpenAI / ChatGPT
# ─────────────────────────────────────────────────────────────────────────────
def detect_openai() -> Optional[DetectedSubscription]:
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return DetectedSubscription(
            id="detected:openai",
            name="OpenAI",
            service="OpenAI",
            source_label="Environment ($OPENAI_API_KEY)",
            account_info="API Key Authentication",
            spec_id="openai",
            scheme=ApiScheme.OPENAI_CHAT,
            base_url="https://api.openai.com/v1",
            api_key=env_key,
            auth=AuthStyle.BEARER,
            recommended_model="gpt-4o",
            extra_models=["gpt-4o", "gpt-4o-mini", "o3-mini"],
        )

    for base in _get_candidate_dirs():
        key_file = base / ".openai" / "api_key"
        if key_file.is_file():
            try:
                k = key_file.read_text(encoding="utf-8").strip()
                if k.startswith("sk-"):
                    return DetectedSubscription(
                        id="detected:openai",
                        name="OpenAI",
                        service="OpenAI",
                        source_label=f"OpenAI CLI ({key_file.name})",
                        account_info="Saved API Key",
                        spec_id="openai",
                        scheme=ApiScheme.OPENAI_CHAT,
                        base_url="https://api.openai.com/v1",
                        api_key=k,
                        auth=AuthStyle.BEARER,
                        recommended_model="gpt-4o",
                    )
            except Exception:
                pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Local Inference Servers (Ollama, LM Studio)
# ─────────────────────────────────────────────────────────────────────────────
def probe_local_server_sync(url: str, timeout_s: float = 0.25) -> Optional[dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Rackhand/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


async def detect_local_servers() -> list[DetectedSubscription]:
    results: list[DetectedSubscription] = []

    # 1. Ollama (running locally on Windows/Linux)
    ollama_data = await asyncio.to_thread(probe_local_server_sync, "http://127.0.0.1:11434/api/tags", 0.3)
    if not ollama_data:
        ollama_data = await asyncio.to_thread(probe_local_server_sync, "http://localhost:11434/api/tags", 0.3)

    if ollama_data and "models" in ollama_data and isinstance(ollama_data["models"], list):
        models = [m.get("name") for m in ollama_data["models"] if isinstance(m, dict) and m.get("name")]
        rec_model = models[0] if models else "llama3.3:latest"
        results.append(DetectedSubscription(
            id="detected:ollama",
            name="Ollama (Local Engine)",
            service="Ollama",
            source_label="Local Service (127.0.0.1:11434)",
            account_info=f"{len(models)} local model{'s' if len(models) != 1 else ''} running",
            spec_id="ollama",
            scheme=ApiScheme.OLLAMA,
            base_url="http://127.0.0.1:11434",
            api_key="",
            auth=AuthStyle.NONE,
            recommended_model=rec_model,
            extra_models=models[:10],
        ))

    # 2. LM Studio (running locally on Windows default port 1234)
    lms_data = await asyncio.to_thread(probe_local_server_sync, "http://127.0.0.1:1234/v1/models", 0.3)
    if lms_data and "data" in lms_data and isinstance(lms_data["data"], list):
        models = [m.get("id") for m in lms_data["data"] if isinstance(m, dict) and m.get("id")]
        rec_model = models[0] if models else "local-model"
        results.append(DetectedSubscription(
            id="detected:lmstudio",
            name="LM Studio (Local)",
            service="LM Studio",
            source_label="Local Server (127.0.0.1:1234)",
            account_info=f"{len(models)} loaded model{'s' if len(models) != 1 else ''}",
            spec_id="lmstudio",
            scheme=ApiScheme.OPENAI_CHAT,
            base_url="http://127.0.0.1:1234/v1",
            api_key="lm-studio",
            auth=AuthStyle.BEARER,
            recommended_model=rec_model,
            extra_models=models[:10],
        ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 7. Other popular providers via standard environment variables
# ─────────────────────────────────────────────────────────────────────────────
def detect_other_env_providers() -> list[DetectedSubscription]:
    out: list[DetectedSubscription] = []

    env_map = [
        ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", ApiScheme.OPENAI_CHAT, "https://api.deepseek.com/v1", "deepseek-chat"),
        ("openrouter", "OpenRouter", "OPENROUTER_API_KEY", ApiScheme.OPENAI_CHAT, "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5.5"),
        ("xai", "xAI (Grok)", "XAI_API_KEY", ApiScheme.OPENAI_CHAT, "https://api.x.ai/v1", "grok-2-1212"),
        ("mistral", "Mistral AI", "MISTRAL_API_KEY", ApiScheme.OPENAI_CHAT, "https://api.mistral.ai/v1", "mistral-large-4-0"),
        ("groq", "Groq LPU", "GROQ_API_KEY", ApiScheme.OPENAI_CHAT, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        ("codex", "Codex LB", "CODEX_LB_API_KEY", ApiScheme.OPENAI_CHAT, "https://api.openai.com/v1", "gpt-4o"),
    ]

    for prov_id, name, env_var, scheme, url, rec_model in env_map:
        val = os.environ.get(env_var)
        if val:
            out.append(DetectedSubscription(
                id=f"detected:{prov_id}",
                name=name,
                service=name,
                source_label=f"Environment (${env_var})",
                account_info="API Key Authentication",
                spec_id=prov_id,
                scheme=scheme,
                base_url=url,
                api_key=val,
                auth=AuthStyle.BEARER,
                recommended_model=rec_model,
            ))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main Detection Entry Points
# ─────────────────────────────────────────────────────────────────────────────
def _dedupe_subscriptions(subs: list[DetectedSubscription]) -> list[DetectedSubscription]:
    out: list[DetectedSubscription] = []
    seen_ids: set[str] = set()
    for s in subs:
        if s.id in seen_ids:
            continue
        seen_ids.add(s.id)
        out.append(s)
    return out


async def detect_all_subscriptions() -> list[DetectedSubscription]:
    """Asynchronously scan the system for all active AI subscriptions and local servers."""
    results: list[DetectedSubscription] = []

    # 1. Antigravity
    ant = detect_antigravity()
    if ant:
        results.append(ant)

    # 2. Claude
    claude = detect_claude()
    if claude:
        results.append(claude)

    # 3. OpenCode
    for oc in detect_opencode():
        results.append(oc)

    # 4. Copilot
    cop = detect_github_copilot()
    if cop:
        results.append(cop)

    # 5. OpenAI
    oai = detect_openai()
    if oai:
        results.append(oai)

    # 6. Other env variables
    results.extend(detect_other_env_providers())

    # 7. Local inference engines (Ollama, LM Studio)
    try:
        local_engines = await detect_local_servers()
        results.extend(local_engines)
    except Exception:
        pass

    return _dedupe_subscriptions(results)


def detect_all_subscriptions_sync() -> list[DetectedSubscription]:
    """Synchronous version for immediate UI loading."""
    results: list[DetectedSubscription] = []

    ant = detect_antigravity()
    if ant:
        results.append(ant)

    claude = detect_claude()
    if claude:
        results.append(claude)

    for oc in detect_opencode():
        results.append(oc)

    cop = detect_github_copilot()
    if cop:
        results.append(cop)

    oai = detect_openai()
    if oai:
        results.append(oai)

    results.extend(detect_other_env_providers())

    return _dedupe_subscriptions(results)
