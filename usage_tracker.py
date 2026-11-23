"""Provider subscription rolling usage windows, rate limits, and quota tracking for Rackhand.

Provides:
- Live subscription rolling usage window fetching (Antigravity 5-hour & weekly limits, Claude 5-hour & 7-day limits, Copilot).
- Window cooldown countdowns to exact reset timestamps.
- API quota and credit balance querying (OpenRouter).
- Dynamic API rate-limit header tracking (RPM, TPM, reset countdowns from Anthropic, OpenAI, Groq).
- Model context window usage metering.
- Disk persistence across sessions (~/.rackhand/usage_history.json).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Optional

import httpx
from rich.text import Text

from models import ApiScheme, ChatSession, ProviderConfig, ensure_agy_in_path, find_agy_executable

logger = logging.getLogger("rackhand.usage")


@dataclass
class SubscriptionUsageWindow:
    """A provider's rolling subscription usage window and cooldown timer."""
    group: str                           # Group/Service: "Gemini Models", "Claude and GPT models", "Claude Subscription Quota", etc.
    name: str                            # Bucket name: "Five Hour Limit Remaining", "Weekly Limit Remaining", "5-Hour Session Window"
    window: str                          # "5h", "weekly", "1m", "day"
    remaining_fraction: float            # 0.0 to 1.0 (e.g. 0.84 -> 84% remaining)
    reset_time: Optional[str] = None     # ISO 8601 string or date: "2026-11-23T17:32:39Z"
    cooldown_seconds: Optional[float] = None  # Seconds until cooldown expires
    cooldown_display: str = ""           # Human-readable countdown: "4h 51m"
    description: str = ""                # Official description/message: "You have used some of your 5-hour limit..."
    models_covered: str = ""             # Models in this group: "Gemini 3.8 Flash" or "Claude Sonnet 4.6, Claude Opus 4.6..."

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, self.remaining_fraction * 100.0))

    @property
    def used_percent(self) -> float:
        return 100.0 - self.remaining_percent


@dataclass
class TokenUsageEntry:
    timestamp: float
    provider_key: str
    model: str
    tokens_in: int
    tokens_out: int
    session_id: str

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp,
            "prov": self.provider_key,
            "model": self.model,
            "in": self.tokens_in,
            "out": self.tokens_out,
            "sess": self.session_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenUsageEntry:
        return cls(
            timestamp=float(d.get("ts", time.time())),
            provider_key=str(d.get("prov", "")),
            model=str(d.get("model", "")),
            tokens_in=int(d.get("in", 0)),
            tokens_out=int(d.get("out", 0)),
            session_id=str(d.get("sess", "")),
        )


@dataclass
class RateLimitInfo:
    limit_requests: Optional[int] = None
    remaining_requests: Optional[int] = None
    reset_requests_s: Optional[float] = None
    limit_tokens: Optional[int] = None
    remaining_tokens: Optional[int] = None
    reset_tokens_s: Optional[float] = None
    last_updated: float = 0.0

    @property
    def has_data(self) -> bool:
        return self.remaining_requests is not None or self.remaining_tokens is not None


@dataclass
class ApiQuotaInfo:
    credits_used: Optional[float] = None
    credits_limit: Optional[float] = None
    credits_remaining: Optional[float] = None
    is_free_tier: Optional[bool] = None
    plan_name: Optional[str] = None
    rate_limit_desc: Optional[str] = None
    status_msg: str = ""
    last_checked: float = 0.0

    @property
    def has_data(self) -> bool:
        return (
            self.credits_remaining is not None
            or self.credits_used is not None
            or bool(self.rate_limit_desc)
        )


@dataclass
class UsageSnapshot:
    provider_name: str
    provider_key: str
    model: str
    base_url: str
    context_window: int
    max_output: int
    session_tokens_in: int
    session_tokens_out: int
    subscription_windows: list[SubscriptionUsageWindow]
    rate_limits: RateLimitInfo
    api_quota: ApiQuotaInfo

    @property
    def session_total_tokens(self) -> int:
        return self.session_tokens_in + self.session_tokens_out

    @property
    def context_percent(self) -> float:
        if self.context_window > 0:
            return min(100.0, (self.session_total_tokens / self.context_window) * 100.0)
        return 0.0

    @property
    def is_subscription(self) -> bool:
        """True if the provider operates on subscription rolling windows rather than API credits."""
        spec = (self.provider_key or "").lower()
        base = (self.base_url or "").lower()
        name = (self.provider_name or "").lower()
        if "antigravity" in spec or "antigravity" in name or "copilot" in spec or "local:agy" in base:
            return True
        if self.subscription_windows:
            return True
        return False


def _parse_int_safe(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except Exception:
        return None


def _parse_reset_seconds(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().lower()
    try:
        return float(s)
    except Exception:
        pass
    # Support "2s", "1m30s", "500ms"
    try:
        total = 0.0
        m_ms = re.search(r"(\d+(?:\.\d+)?)\s*ms", s)
        if m_ms:
            total += float(m_ms.group(1)) / 1000.0
            s = s.replace(m_ms.group(0), "")
        m_m = re.search(r"(\d+(?:\.\d+)?)\s*m", s)
        if m_m:
            total += float(m_m.group(1)) * 60.0
            s = s.replace(m_m.group(0), "")
        m_s = re.search(r"(\d+(?:\.\d+)?)\s*s", s)
        if m_s:
            total += float(m_s.group(1))
        return total if total > 0 else None
    except Exception:
        return None


def _format_seconds_to_hms(total_seconds: float) -> str:
    secs = int(max(0.0, total_seconds))
    if secs <= 0:
        return "Ready"
    days = secs // 86400
    secs %= 86400
    hours = secs // 3600
    secs %= 3600
    mins = secs // 60
    s_rem = secs % 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if mins > 0 or (days == 0 and hours == 0):
        parts.append(f"{mins}m")
    if days == 0 and hours == 0 and mins < 5 and s_rem > 0:
        parts.append(f"{s_rem}s")
    return " ".join(parts) if parts else "0m"


def parse_cooldown(reset_time_val: Any, now_ts: Optional[float] = None) -> tuple[Optional[float], str]:
    """Parse an ISO 8601 timestamp or seconds value into (cooldown_seconds, human_display)."""
    if not reset_time_val:
        return None, "Active / Ready"

    if now_ts is None:
        now_ts = time.time()

    if isinstance(reset_time_val, (int, float)):
        sec = float(reset_time_val)
        if sec <= 0:
            return 0.0, "Ready (Refreshed)"
        return sec, _format_seconds_to_hms(sec)

    s = str(reset_time_val).strip()
    # Try ISO 8601 timestamp e.g. "2026-11-23T17:32:39Z"
    try:
        clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        diff = dt.timestamp() - now_ts
        if diff <= 0:
            return 0.0, "Ready (Refreshed)"
        return diff, _format_seconds_to_hms(diff)
    except Exception:
        pass

    # Try numeric seconds string
    parsed_s = _parse_reset_seconds(s)
    if parsed_s is not None:
        return parsed_s, _format_seconds_to_hms(parsed_s)

    return None, s


def _find_agy_executable() -> Optional[str]:
    """Locate the agy CLI binary on Linux, macOS, or Windows."""
    return find_agy_executable()


class UsageTracker:
    """Central usage, rate limit, and rolling subscription windows tracker."""

    _instance: Optional[UsageTracker] = None

    def __init__(self, storage_path: Optional[str] = None) -> None:
        self.storage_path = Path(storage_path or os.path.expanduser("~/.rackhand/usage_history.json"))
        self.entries: list[TokenUsageEntry] = []
        self.rate_limits: dict[str, RateLimitInfo] = {}
        self.api_quotas: dict[str, ApiQuotaInfo] = {}
        self.cached_subscription_windows: dict[str, list[SubscriptionUsageWindow]] = {}
        self._last_fetch_ts: dict[str, float] = {}
        self._load_from_disk()

    @classmethod
    def get_instance(cls) -> UsageTracker:
        if cls._instance is None:
            cls._instance = UsageTracker()
        return cls._instance

    def record_turn_usage(
        self,
        provider_key: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        session_id: str = "",
        headers: Optional[Any] = None,
    ) -> None:
        """Record completed turn token consumption and parse rate limit headers."""
        if tokens_in > 0 or tokens_out > 0:
            entry = TokenUsageEntry(
                timestamp=time.time(),
                provider_key=provider_key,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                session_id=session_id,
            )
            self.entries.append(entry)
            if len(self.entries) > 5000:
                self.entries = self.entries[-5000:]
            self._save_to_disk()

        if headers:
            self.update_rate_limits_from_headers(provider_key, headers)

    def update_rate_limits_from_headers(self, provider_key: str, headers: Any) -> None:
        """Parse rate limit headers from HTTP response headers (OpenAI, Anthropic, Groq)."""
        info = self.rate_limits.setdefault(provider_key, RateLimitInfo())
        h = {str(k).lower(): str(v) for k, v in dict(headers).items()}

        # 1. Anthropic headers
        if "anthropic-ratelimit-requests-limit" in h or "anthropic-ratelimit-tokens-limit" in h:
            info.limit_requests = _parse_int_safe(h.get("anthropic-ratelimit-requests-limit"))
            info.remaining_requests = _parse_int_safe(h.get("anthropic-ratelimit-requests-remaining"))
            info.reset_requests_s = _parse_reset_seconds(h.get("anthropic-ratelimit-requests-reset"))

            info.limit_tokens = _parse_int_safe(h.get("anthropic-ratelimit-tokens-limit"))
            info.remaining_tokens = _parse_int_safe(h.get("anthropic-ratelimit-tokens-remaining"))
            info.reset_tokens_s = _parse_reset_seconds(h.get("anthropic-ratelimit-tokens-reset"))
            info.last_updated = time.time()
            return

        # 2. OpenAI / Groq / OpenRouter / Cloud standard headers
        if "x-ratelimit-limit-requests" in h or "x-ratelimit-limit-tokens" in h:
            info.limit_requests = _parse_int_safe(h.get("x-ratelimit-limit-requests"))
            info.remaining_requests = _parse_int_safe(h.get("x-ratelimit-remaining-requests"))
            info.reset_requests_s = _parse_reset_seconds(h.get("x-ratelimit-reset-requests"))

            info.limit_tokens = _parse_int_safe(h.get("x-ratelimit-limit-tokens"))
            info.remaining_tokens = _parse_int_safe(h.get("x-ratelimit-remaining-tokens"))
            info.reset_tokens_s = _parse_reset_seconds(h.get("x-ratelimit-reset-tokens"))
            info.last_updated = time.time()
            return

    def get_rate_limits(self, provider_key: str) -> RateLimitInfo:
        return self.rate_limits.get(provider_key, RateLimitInfo())

    def get_api_quota(self, provider_key: str) -> ApiQuotaInfo:
        return self.api_quotas.get(provider_key, ApiQuotaInfo())

    # ─────────────────────────────────────────────────────────────────────────
    # Subscription Rolling Usage Windows Fetchers
    # ─────────────────────────────────────────────────────────────────────────

    async def fetch_antigravity_windows(self) -> list[SubscriptionUsageWindow]:
        """Fetch the official rolling 5-hour and weekly subscription quota windows from Antigravity."""
        agy_bin = _find_agy_executable()
        if not agy_bin:
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                agy_bin, "-p", "/usage", "--output-format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=9.0)
            if proc.returncode != 0 or not stdout:
                return []

            data = json.loads(stdout.decode("utf-8", errors="replace"))
            cmd_data = data.get("command", {}).get("data", {})
            groups = cmd_data.get("groups", [])
            windows: list[SubscriptionUsageWindow] = []

            now_ts = time.time()
            for g in groups:
                g_name = g.get("name", "Antigravity Models")
                models_desc = g.get("description", "")
                for b in g.get("buckets", []):
                    b_name = b.get("name", "Quota Window")
                    b_win = b.get("window", "")
                    rem_frac = float(b.get("remaining_fraction", 1.0))
                    reset_t = b.get("reset_time")
                    desc = b.get("description", "")
                    cooldown_s, cooldown_disp = parse_cooldown(reset_t, now_ts)

                    windows.append(
                        SubscriptionUsageWindow(
                            group=f"Google Antigravity ({g_name})",
                            name=b_name,
                            window=b_win,
                            remaining_fraction=rem_frac,
                            reset_time=reset_t,
                            cooldown_seconds=cooldown_s,
                            cooldown_display=cooldown_disp,
                            description=desc,
                            models_covered=models_desc,
                        )
                    )
            return windows
        except Exception as exc:
            logger.debug("Failed to query Antigravity /usage: %s", exc)
            return []

    async def fetch_claude_windows(self, api_key_or_token: str = "") -> list[SubscriptionUsageWindow]:
        """Fetch Claude subscription rolling 5-hour session and weekly limits."""
        windows: list[SubscriptionUsageWindow] = []
        token = api_key_or_token.strip()

        if not token:
            token = (
                os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_API_KEY")
                or ""
            )

        if token:
            try:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/0.2.29",
                }
                if token.startswith("sk-ant-"):
                    headers["x-api-key"] = token

                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.get("https://api.anthropic.com/api/oauth/usage", headers=headers)
                    if resp.status_code == 200:
                        d = resp.json()
                        now_ts = time.time()
                        # 1. 5-Hour Rolling Session Window
                        five_h = d.get("five_hour") or {}
                        if five_h:
                            util = float(five_h.get("utilization", 0.0))
                            rem_frac = max(0.0, min(1.0, 1.0 - (util / 100.0) if util > 1.0 else (1.0 - util)))
                            resets_at = five_h.get("resets_at")
                            cd_s, cd_disp = parse_cooldown(resets_at, now_ts)
                            windows.append(
                                SubscriptionUsageWindow(
                                    group="Claude Subscription Quota",
                                    name="5-Hour Session Window",
                                    window="5h",
                                    remaining_fraction=rem_frac,
                                    reset_time=resets_at,
                                    cooldown_seconds=cd_s,
                                    cooldown_display=cd_disp,
                                    description="Rolling 5-hour window. Prompts clear continuously as they pass the 5-hour mark.",
                                    models_covered="Claude 3.7 Sonnet, Claude 3.5 Sonnet, Claude Opus",
                                )
                            )

                        # 2. 7-Day / Weekly Quota
                        seven_d = d.get("seven_day") or d.get("weekly") or {}
                        if seven_d:
                            util = float(seven_d.get("utilization", 0.0))
                            rem_frac = max(0.0, min(1.0, 1.0 - (util / 100.0) if util > 1.0 else (1.0 - util)))
                            resets_at = seven_d.get("resets_at")
                            cd_s, cd_disp = parse_cooldown(resets_at, now_ts)
                            windows.append(
                                SubscriptionUsageWindow(
                                    group="Claude Subscription Quota",
                                    name="Weekly Limit Remaining",
                                    window="weekly",
                                    remaining_fraction=rem_frac,
                                    reset_time=resets_at,
                                    cooldown_seconds=cd_s,
                                    cooldown_display=cd_disp,
                                    description="7-day rolling plan baseline limit.",
                                    models_covered="Claude 3.7 Sonnet, Claude 3.5 Sonnet, Claude Opus",
                                )
                            )
            except Exception as exc:
                logger.debug("Claude oauth usage fetch failed: %s", exc)

        return windows

    async def fetch_copilot_windows(self, token: str = "") -> list[SubscriptionUsageWindow]:
        """Fetch GitHub Copilot rate limit windows and reset cooldowns."""
        if not token:
            token = os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            try:
                from subscription_detector import _get_candidate_dirs
                for base in _get_candidate_dirs():
                    p = base / ".config" / "gh" / "hosts.yml"
                    if p.is_file():
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        m = re.search(r"oauth_token:\s*([^\s]+)", content)
                        if m:
                            token = m.group(1)
                            break
            except Exception:
                pass

        if not token:
            return []

        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(
                    "https://api.github.com/rate_limit",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": "Rackhand"},
                )
                if resp.status_code == 200:
                    d = resp.json().get("resources", {})
                    windows: list[SubscriptionUsageWindow] = []
                    now_ts = time.time()
                    for key, label in [("copilot_usage_records", "Copilot Usage Records"), ("core", "GitHub API Core")]:
                        res = d.get(key)
                        if res and res.get("limit"):
                            lim = int(res["limit"])
                            rem = int(res.get("remaining", lim))
                            rst = int(res.get("reset", 0))
                            rem_frac = float(rem) / float(lim)
                            diff = max(0.0, rst - now_ts) if rst else 0.0
                            cd_disp = _format_seconds_to_hms(diff)
                            iso_str = datetime.fromtimestamp(rst, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if rst else ""
                            windows.append(
                                SubscriptionUsageWindow(
                                    group="GitHub Copilot Subscription",
                                    name=label,
                                    window="1h",
                                    remaining_fraction=rem_frac,
                                    reset_time=iso_str,
                                    cooldown_seconds=diff,
                                    cooldown_display=cd_disp,
                                    description=f"{rem:,} / {lim:,} requests remaining. Window resets at {iso_str}.",
                                )
                            )
                    return windows
        except Exception as exc:
            logger.debug("Failed to fetch Copilot rate limits: %s", exc)
        return []

    async def fetch_openrouter_quota(self, provider: ProviderConfig) -> ApiQuotaInfo:
        """Fetch live OpenRouter credit balance and rate limits."""
        key_id = provider.key or provider.spec_id or "default"
        info = self.api_quotas.setdefault(key_id, ApiQuotaInfo())
        api_key = provider.api_key.strip() if isinstance(provider.api_key, str) else ""

        if not api_key:
            info.status_msg = "No API key configured"
            return info

        try:
            async with httpx.AsyncClient(timeout=7.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/auth/key",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    usage = data.get("usage")
                    limit = data.get("limit")
                    info.credits_used = float(usage) if usage is not None else None
                    info.credits_limit = float(limit) if limit is not None else None
                    if info.credits_limit is not None and info.credits_used is not None:
                        info.credits_remaining = max(0.0, info.credits_limit - info.credits_used)
                    info.is_free_tier = bool(data.get("is_free_tier", False))
                    rate_limit = data.get("rate_limit", {})
                    if rate_limit.get("requests"):
                        info.rate_limit_desc = f"{rate_limit['requests']} req / {rate_limit.get('interval', '10s')}"
                    info.plan_name = "OpenRouter Account Key"
                    info.status_msg = "Authenticated"
                    info.last_checked = time.time()
        except Exception as exc:
            info.status_msg = f"Quota query failed: {exc}"
        return info

    async def fetch_provider_telemetry(
        self,
        provider: Optional[ProviderConfig] = None,
        force_refresh: bool = False,
    ) -> None:
        """Fetch telemetry specific to the active provider (subscription windows OR API quota)."""
        prov_key = provider.key if provider else "default"
        now = time.time()
        if not force_refresh and (now - self._last_fetch_ts.get(prov_key, 0.0) < 60.0):
            return

        self._last_fetch_ts[prov_key] = now
        spec = (provider.spec_id if provider else "") or ""
        scheme = provider.scheme if provider else ApiScheme.OPENAI_CHAT
        base_url = (provider.base_url if provider else "").lower()
        prov_name = (provider.name if provider else "").lower()

        # 1. Antigravity Subscription
        if spec == "antigravity" or scheme == ApiScheme.ANTIGRAVITY or "local:agy" in base_url or "antigravity" in prov_name:
            agy_wins = await self.fetch_antigravity_windows()
            self.cached_subscription_windows[prov_key] = agy_wins
            return

        # 2. GitHub Copilot Subscription
        if spec == "copilot" or "copilot" in prov_name:
            cop_key = provider.api_key if provider else ""
            cop_wins = await self.fetch_copilot_windows(cop_key)
            self.cached_subscription_windows[prov_key] = cop_wins
            return

        # 3. Claude Subscription or API
        if spec == "anthropic" or scheme == ApiScheme.ANTHROPIC or "anthropic" in prov_name or "claude" in prov_name:
            claude_key = provider.api_key if provider else ""
            # If OAuth token or Claude Code subscription
            if not str(claude_key).startswith("sk-ant-") or "code" in prov_name:
                claude_wins = await self.fetch_claude_windows(claude_key)
                if claude_wins:
                    self.cached_subscription_windows[prov_key] = claude_wins
                    return
            self.cached_subscription_windows[prov_key] = []
            return

        # 4. OpenRouter API
        if "openrouter" in base_url or spec == "openrouter" or "openrouter" in prov_name:
            self.cached_subscription_windows[prov_key] = []
            if provider:
                await self.fetch_openrouter_quota(provider)
            return

        # 5. Other API Providers (OpenAI, DeepSeek, Groq, Ollama, etc.)
        self.cached_subscription_windows[prov_key] = []

    # Alias for backwards compatibility
    async def fetch_all_subscription_windows(
        self,
        provider: Optional[ProviderConfig] = None,
        force_refresh: bool = False,
    ) -> list[SubscriptionUsageWindow]:
        await self.fetch_provider_telemetry(provider, force_refresh=force_refresh)
        prov_key = provider.key if provider else "default"
        return self.cached_subscription_windows.get(prov_key, [])

    def get_snapshot(
        self,
        provider: Optional[ProviderConfig],
        session: Optional[ChatSession] = None,
        context_window_override: int = 0,
    ) -> UsageSnapshot:
        """Build a complete usage snapshot combining session tokens, subscription windows, and API quotas."""
        prov_key = provider.key if provider else "default"
        prov_name = provider.name if provider else "Default Provider"
        model = provider.model if provider else "default"
        base_url = provider.base_url if provider else ""

        ctx = context_window_override or (provider.context_window if provider else 0)
        if ctx <= 0 and provider:
            ctx = _infer_context_window(provider.model, provider.spec_id)
        if ctx <= 0:
            ctx = 200_000

        max_out = provider.max_output if provider and provider.max_output else 2048

        s_in = session.tokens_in if session else 0
        s_out = session.tokens_out if session else 0

        sub_windows = self.cached_subscription_windows.get(prov_key, [])
        rl = self.rate_limits.get(prov_key, RateLimitInfo())
        quota = self.api_quotas.get(prov_key, ApiQuotaInfo())

        return UsageSnapshot(
            provider_name=prov_name,
            provider_key=prov_key,
            model=model,
            base_url=base_url,
            context_window=ctx,
            max_output=max_out,
            session_tokens_in=s_in,
            session_tokens_out=s_out,
            subscription_windows=sub_windows,
            rate_limits=rl,
            api_quota=quota,
        )

    def _load_from_disk(self) -> None:
        if not self.storage_path.is_file():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.entries = [TokenUsageEntry.from_dict(d) for d in data if isinstance(d, dict)]
        except Exception as exc:
            logger.debug("Failed to load usage history: %s", exc)

    def _save_to_disk(self) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self.entries[-2000:]], f, indent=2)
        except Exception as exc:
            logger.debug("Failed to save usage history: %s", exc)


def _infer_context_window(model: Optional[str], spec_id: Optional[str]) -> int:
    """Estimate context window capacity in tokens based on model name."""
    m = (model or "").lower()
    s = (spec_id or "").lower()

    if "gemini-1.5" in m or "gemini-2.0" in m or "gemini-2.5" in m or "gemini-3.8" in m or "gemini" in s:
        return 1_000_000
    if "claude" in m or "claude" in s or "anthropic" in s:
        return 200_000
    if "gpt-4o" in m or "o1" in m or "o3" in m:
        return 128_000
    if "gpt-4-turbo" in m or "gpt-4-32k" in m:
        return 128_000
    if "gpt-4" in m:
        return 8_192
    if "gpt-3.5-turbo" in m:
        return 16_385
    if "deepseek" in m or "deepseek" in s:
        return 64_000
    if "llama-3.1" in m or "llama-3.2" in m or "llama-3.3" in m:
        return 128_000
    if "mistral-large" in m or "codestral" in m:
        return 128_000
    if "qwen-2.5" in m:
        return 128_000
    return 200_000
