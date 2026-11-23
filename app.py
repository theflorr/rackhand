#!/usr/bin/env python3
"""
Rackhand - Agentic network configuration for the terminal.

Usage:
    python app.py
    python -m app
"""
from __future__ import annotations

from typing import Optional

from textual.app import App
from textual.binding import Binding

from backend import Backend, RealBackend
from models import DeviceSession, ProviderConfig, Settings, ensure_agy_in_path
from ui import APP_CSS, WorkspaceScreen, _rackhand_theme


class RackhandApp(App):
    TITLE = "Rackhand"
    SUB_TITLE = "agentic network configuration"
    CSS = APP_CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=True)]

    def __init__(self, backend: Optional[Backend] = None) -> None:
        super().__init__()
        self.backend: Backend = backend or RealBackend()
        self.sessions: list[DeviceSession] = []
        self.active_id: Optional[str] = None
        self.provider: Optional[ProviderConfig] = None
        self.settings = Settings()

    def action_help_quit(self) -> None:
        self.notify("Press Ctrl+Q to exit", title="", severity="information", timeout=2.5)

    @property
    def active(self) -> Optional[DeviceSession]:
        return next((s for s in self.sessions if s.id == self.active_id), None)

    def apply_settings(self, s: Settings) -> None:
        self.settings = s
        self.run_worker(self.backend.save_settings(s), group="save-settings")
        try:
            self.theme = s.theme
        except Exception:
            pass

    async def on_mount(self) -> None:
        ensure_agy_in_path()
        self.settings = await self.backend.load_settings()
        
        # Auto-detect and load local subscriptions (Claude, Antigravity, OpenCode, Copilot, etc.)
        try:
            await self.backend.auto_import_detected()
        except Exception:
            pass

        configs = await self.backend.load_provider_configs()
        if not self.provider:
            # Prioritize verified active providers (Antigravity with AGY CLI, OpenRouter, etc.)
            for preferred_key in ["antigravity", "openrouter", "deepseek", "anthropic", "openai", "gemini"]:
                cfg = configs.get(preferred_key)
                if cfg and cfg.api_key:
                    self.provider = cfg
                    break
            if not self.provider and configs:
                self.provider = next(iter(configs.values()))
        try:
            self.register_theme(_rackhand_theme())
            self.theme = self.settings.theme
        except Exception:
            pass

        # Auto-connect primary device on boot so user lands straight into live session
        profiles = await self.backend.load_profiles()
        if profiles:
            target = profiles[0]
            try:
                s = await self.backend.connect(target, {})
                self.sessions.append(s)
                self.active_id = s.id
            except Exception:
                pass

        await self.push_screen(WorkspaceScreen())


def main() -> None:
    backend = RealBackend()
    app = RackhandApp(backend=backend)
    app.run()


if __name__ == "__main__":
    main()
