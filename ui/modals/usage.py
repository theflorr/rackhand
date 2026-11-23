"""Subscription rolling usage windows, cooldowns, and live limits modal dialog."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Optional

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from models import ChatSession, ProviderConfig
from usage_tracker import SubscriptionUsageWindow, UsageSnapshot, UsageTracker

logger = logging.getLogger("rackhand.usage")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.2f}M" if (val * 100) % 100 != 0 else f"{int(val)}M"
    if n >= 1_000:
        val = n / 1_000
        return f"{val:.1f}k" if (val * 10) % 10 != 0 else f"{int(val)}k"
    return f"{n:,}"


def _make_remaining_bar(pct: float, width: int = 28) -> Text:
    """Render a visual progress bar representing remaining capacity percentage."""
    clamped = max(0.0, min(100.0, pct))
    filled_len = int(round((clamped / 100.0) * width))
    empty_len = width - filled_len

    if clamped >= 50.0:
        fill_style = "bold #e4e4e7"  # Clean white/silver
    elif clamped >= 20.0:
        fill_style = "bold #d4d4d8"  # Slate warning
    else:
        fill_style = "bold #f87171"  # Muted red critical

    t = Text()
    t.append("[", style="#555555")
    t.append("█" * filled_len, style=fill_style)
    t.append("░" * empty_len, style="#262626")
    t.append("] ", style="#555555")
    t.append(f"{clamped:.1f}% Remaining", style=fill_style)
    return t


def _make_context_bar(pct: float, width: int = 28) -> Text:
    """Render a visual progress bar representing context window occupancy."""
    clamped = max(0.0, min(100.0, pct))
    filled_len = int(round((clamped / 100.0) * width))
    empty_len = width - filled_len

    if clamped >= 90.0:
        fill_style = "bold #f87171"
    elif clamped >= 70.0:
        fill_style = "bold #d4d4d8"
    else:
        fill_style = "bold #e4e4e7"

    t = Text()
    t.append("[", style="#555555")
    t.append("█" * filled_len, style=fill_style)
    t.append("░" * empty_len, style="#262626")
    t.append("] ", style="#555555")
    t.append(f"{clamped:.1f}% Used", style=fill_style)
    return t


class UsageModal(ModalScreen[None]):
    """Modal displaying provider subscription rolling windows, cooldown timers, or API limits."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(
        self,
        tracker: UsageTracker,
        provider: Optional[ProviderConfig],
        session: Optional[ChatSession] = None,
        on_post_to_chat: Optional[Any] = None,
    ) -> None:
        super().__init__()
        self.tracker = tracker
        self.provider = provider
        self.session = session
        self.on_post_to_chat = on_post_to_chat
        self._is_refreshing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="usage-dialog", classes="dialog"):
            with Vertical(id="usage-hdr"):
                hdr = Text()
                hdr.append("› ", style="bold #cccccc")
                hdr.append("PROVIDER USAGE & LIMITS", style="bold white")
                yield Static(hdr, id="usage-title")

                p_name = self.provider.name if self.provider else "None"
                model_name = self.provider.model if self.provider and self.provider.model else "default"
                yield Static(f"Active Provider: {p_name}  ·  Model: {model_name}", id="usage-subtitle")

            with VerticalScroll(id="usage-scroll"):
                # 1. Model & Session Context (Always displayed)
                with Vertical(classes="settings-card", id="usage-card-context"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("ACTIVE MODEL & SESSION CONTEXT", classes="settings-card-title")
                        yield Static("Session context occupancy and model token boundaries.", classes="settings-card-desc")
                    yield Static(id="usage-context-content")

                # 2. Subscription Rolling Windows & Cooldowns (Shown ONLY for Subscriptions)
                with Vertical(classes="settings-card", id="usage-card-windows"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("SUBSCRIPTION ROLLING WINDOWS & COOLDOWNS", classes="settings-card-title")
                        yield Static(
                            "Official provider subscription quotas (5-hour session sprints, weekly caps, and cooldown timers).",
                            classes="settings-card-desc",
                        )
                    yield Static(id="usage-windows-content")

                # 3. API Credits & Rate Limits (Shown ONLY for API Credit / Pay-Per-Token Providers)
                with Vertical(classes="settings-card", id="usage-card-api"):
                    with Vertical(classes="settings-card-header"):
                        yield Static("API CREDITS & RATE LIMITS", classes="settings-card-title")
                        yield Static("Account balance, remaining credits, and API rate limits.", classes="settings-card-desc")
                    yield Static(id="usage-api-content")

            with Horizontal(classes="settings-btn-row"):
                yield Button("Refresh limits", variant="primary", id="btn-usage-refresh")
                if self.on_post_to_chat:
                    yield Button("Post to Chat", id="btn-usage-post")
                yield Button("Close (Esc)", id="btn-usage-close")

    async def on_mount(self) -> None:
        self._render_snapshot()
        self._fetch_fresh_quota()

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-usage-close")
    def _close_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-usage-refresh")
    def _refresh_pressed(self) -> None:
        if not self._is_refreshing:
            self.notify("Fetching provider telemetry and limits...", title="Usage")
            self._fetch_fresh_quota()

    @on(Button.Pressed, "#btn-usage-post")
    def _post_pressed(self) -> None:
        if self.on_post_to_chat:
            snap = self.tracker.get_snapshot(self.provider, self.session)
            self.on_post_to_chat(snap)
            self.notify("Usage snapshot posted to active transcript.", title="Usage")
            self.dismiss(None)

    @work(group="usage_fetch")
    async def _fetch_fresh_quota(self) -> None:
        self._is_refreshing = True
        try:
            btn = self.query_one("#btn-usage-refresh", Button)
            btn.disabled = True
            btn.label = "Refreshing..."
        except Exception:
            pass

        try:
            await self.tracker.fetch_provider_telemetry(self.provider, force_refresh=True)
        except Exception as exc:
            logger.debug("Provider telemetry fetch failed: %s", exc)
        finally:
            self._is_refreshing = False
            try:
                btn = self.query_one("#btn-usage-refresh", Button)
                btn.disabled = False
                btn.label = "Refresh limits"
            except Exception:
                pass
            self._render_snapshot()

    def _render_snapshot(self) -> None:
        snap = self.tracker.get_snapshot(self.provider, self.session)
        is_sub = snap.is_subscription

        # Dynamic visibility: Show EITHER subscription windows OR API credits/limits!
        try:
            self.query_one("#usage-card-windows").display = is_sub
            self.query_one("#usage-card-api").display = not is_sub
        except Exception:
            pass

        # ─────────────────────────────────────────────────────────────────────
        # 1. Render Session Context
        # ─────────────────────────────────────────────────────────────────────
        ctx_text = Text()
        ctx_text.append(f"Model: {snap.model}  ", style="bold white")
        ctx_text.append(f"·  Context Limit: {_fmt_tokens(snap.context_window)} tokens  ", style="#888888")
        ctx_text.append(f"·  Max Output: {_fmt_tokens(snap.max_output)} tokens\n\n", style="#888888")

        ctx_text.append("Session Context Occupancy:\n", style="bold #cccccc")
        ctx_text.append(f"  {_fmt_tokens(snap.session_total_tokens)} / {_fmt_tokens(snap.context_window)} tokens  ")
        ctx_text.append_text(_make_context_bar(snap.context_percent, width=30))
        ctx_text.append(
            f"\n  (Prompt: {_fmt_tokens(snap.session_tokens_in)}  ·  Completion: {_fmt_tokens(snap.session_tokens_out)})\n",
            style="dim #777777",
        )

        try:
            self.query_one("#usage-context-content", Static).update(ctx_text)
        except Exception:
            pass

        # ─────────────────────────────────────────────────────────────────────
        # 2. Render Subscription Rolling Windows (if subscription)
        # ─────────────────────────────────────────────────────────────────────
        if is_sub:
            win_text = Text()
            sub_windows = snap.subscription_windows

            if sub_windows:
                grouped: dict[str, list[SubscriptionUsageWindow]] = {}
                for w in sub_windows:
                    grouped.setdefault(w.group, []).append(w)

                first_group = True
                for grp_name, buckets in grouped.items():
                    if not first_group:
                        win_text.append("\n" + "─" * 60 + "\n\n", style="#262626")
                    first_group = False

                    win_text.append(f"● {grp_name}\n", style="bold #e4e4e7")
                    models_info = buckets[0].models_covered if buckets else ""
                    if models_info:
                        win_text.append(f"  {models_info}\n", style="dim #888888")
                    win_text.append("\n")

                    for b in buckets:
                        win_tag = f"[{b.window.upper()}]" if b.window else "[QUOTA]"
                        win_text.append(f"  {b.name}  ", style="bold white")
                        win_text.append(f"{win_tag}\n", style="bold #555555")

                        win_text.append("  ")
                        win_text.append_text(_make_remaining_bar(b.remaining_percent, width=32))
                        win_text.append(f"  ({b.used_percent:.1f}% consumed)\n", style="dim #777777")

                        if b.cooldown_display:
                            win_text.append("  ⏱ Cooldown: ", style="bold #cccccc")
                            if "ready" in b.cooldown_display.lower() or "refresh" in b.cooldown_display.lower():
                                win_text.append(f"{b.cooldown_display}  ", style="bold #e4e4e7")
                            else:
                                win_text.append(f"Resets in {b.cooldown_display}  ", style="bold #d4d4d8")

                            if b.reset_time:
                                win_text.append(f"(Refreshes {b.reset_time})\n", style="dim #888888")
                            else:
                                win_text.append("\n")

                        if b.description:
                            win_text.append(f"  ℹ {b.description}\n", style="dim #666666")
                        win_text.append("\n")
            else:
                if self._is_refreshing:
                    win_text.append("Querying subscription rolling windows...", style="dim #888888")
                else:
                    win_text.append(
                        "No subscription rolling windows returned for active provider.\n"
                        "Verify your subscription or CLI configuration is active, then click 'Refresh limits'.",
                        style="dim #777777",
                    )

            try:
                self.query_one("#usage-windows-content", Static).update(win_text)
            except Exception:
                pass

        # ─────────────────────────────────────────────────────────────────────
        # 3. Render API Credits & Limits (if API provider)
        # ─────────────────────────────────────────────────────────────────────
        if not is_sub:
            api_text = Text()
            quota = snap.api_quota
            rl = snap.rate_limits

            if quota.plan_name:
                api_text.append(f"Plan / Key: {quota.plan_name}  ", style="bold #e4e4e7")
            if quota.status_msg:
                api_text.append(f"({quota.status_msg})\n", style="dim #777777")
            else:
                api_text.append("\n")

            if quota.credits_limit is not None or quota.credits_used is not None:
                api_text.append("\nAccount Credit Balance:\n", style="bold #cccccc")
                rem = quota.credits_remaining if quota.credits_remaining is not None else 0.0
                lim = quota.credits_limit if quota.credits_limit is not None else 0.0
                used = quota.credits_used if quota.credits_used is not None else 0.0
                api_text.append(f"  ${rem:.2f} remaining ", style="bold #e4e4e7" if rem > 5 else "bold #d4d4d8")
                api_text.append(f"of ${lim:.2f} limit (Consumed: ${used:.2f})\n", style="#888888")

            api_text.append("\nLive API Headers (RPM / TPM):\n", style="bold #cccccc")
            if rl.has_data:
                if rl.remaining_requests is not None and rl.limit_requests:
                    pct_rem = (float(rl.remaining_requests) / float(rl.limit_requests)) * 100.0
                    reset_s = f" (Resets in {int(rl.reset_requests_s)}s)" if rl.reset_requests_s else ""
                    api_text.append(f"  Requests: {rl.remaining_requests:,} / {rl.limit_requests:,} RPM remaining{reset_s}\n")
                    api_text.append("  ")
                    api_text.append_text(_make_remaining_bar(pct_rem, width=28))
                    api_text.append("\n")
                elif rl.remaining_requests is not None:
                    api_text.append(f"  Requests remaining: {rl.remaining_requests:,} RPM\n")

                if rl.remaining_tokens is not None and rl.limit_tokens:
                    pct_tok = (float(rl.remaining_tokens) / float(rl.limit_tokens)) * 100.0
                    reset_t = f" (Resets in {int(rl.reset_tokens_s)}s)" if rl.reset_tokens_s else ""
                    api_text.append(f"  Tokens: {rl.remaining_tokens:,} / {rl.limit_tokens:,} TPM remaining{reset_t}\n")
                    api_text.append("  ")
                    api_text.append_text(_make_remaining_bar(pct_tok, width=28))
                    api_text.append("\n")
                elif rl.remaining_tokens is not None:
                    api_text.append(f"  Tokens remaining: {rl.remaining_tokens:,} TPM\n")
            else:
                if quota.rate_limit_desc:
                    api_text.append(f"  Rate Limit: {quota.rate_limit_desc}\n", style="#999999")
                else:
                    api_text.append("  Rate limit headers will populate upon first model inference.\n", style="dim #777777")

            try:
                self.query_one("#usage-api-content", Static).update(api_text)
            except Exception:
                pass
