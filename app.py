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
from ui.modals import UpdateModal
from ui.styles import _rackhand_console_theme
from updater import check_for_update

VERSION = "v1.3.7"


class RackhandApp(App):
    TITLE = "Rackhand"
    SUB_TITLE = f"agentic network configuration · {VERSION}"
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

        from vault import get_vault
        from ui.modals.vault import VaultSetupModal, VaultUnlockModal

        vault = get_vault()
        if not vault.is_unlocked():
            if not vault.is_vault_initialized() or vault.has_unencrypted_data():
                is_mig = vault.has_unencrypted_data()
                await self.push_screen(VaultSetupModal(migration_mode=is_mig), self._on_vault_unlocked)
                return
            else:
                await self.push_screen(VaultUnlockModal(), self._on_vault_unlocked)
                return

        await self._complete_startup()

    async def _on_vault_unlocked(self, success: bool) -> None:
        if not success:
            self.exit()
            return
        await self._complete_startup()

    async def _complete_startup(self) -> None:
        if hasattr(self.backend, "reload_from_vault"):
            self.backend.reload_from_vault()

        self.settings = await self.backend.load_settings()

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
            self.console.push_theme(_rackhand_console_theme())
        except Exception:
            pass

        # Push WorkspaceScreen immediately so user lands in UI with zero delay
        await self.push_screen(WorkspaceScreen())

        # Non-blocking background startup tasks: subscription auto-detection, initial device connect, and update check
        self.run_worker(self._async_startup_background(), group="startup-background")
        self.run_worker(self._check_for_updates(), group="auto-updater")

    async def _async_startup_background(self) -> None:
        # 1. Auto-detect and load local subscriptions (Claude, Antigravity, OpenCode, Copilot, etc.)
        try:
            await self.backend.auto_import_detected()
            if not self.provider:
                configs = await self.backend.load_provider_configs()
                for preferred_key in ["antigravity", "openrouter", "deepseek", "anthropic", "openai", "gemini"]:
                    cfg = configs.get(preferred_key)
                    if cfg and cfg.api_key:
                        self.provider = cfg
                        break
                if not self.provider and configs:
                    self.provider = next(iter(configs.values()))
                if isinstance(self.screen, WorkspaceScreen):
                    self.screen.update_bars()
        except Exception:
            pass

        # 2. Auto-connect primary device profile in background so user lands into live session
        try:
            profiles = await self.backend.load_profiles()
            if profiles and not self.sessions:
                target = profiles[0]
                s = await self.backend.connect(target, {})
                if s not in self.sessions:
                    self.sessions.append(s)
                self.active_id = s.id
                if isinstance(self.screen, WorkspaceScreen):
                    self.screen._start_pump(s)
                    self.screen.refresh_device_list()
                    self.screen.refresh_console_devices()
                    self.screen._render_console()
                    self.screen.update_bars()
        except Exception:
            pass

    async def _check_for_updates(self) -> None:
        try:
            rel = await check_for_update(VERSION)
            if rel:
                await self.push_screen(UpdateModal(current_version=VERSION, release=rel))
        except Exception:
            pass


def _configure_windows_console_icon() -> None:
    """Set Windows console AppUserModelID, title, and taskbar/window icon to Rackhand."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        from pathlib import Path

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        # Separate taskbar grouping from generic cmd.exe / conhost.exe
        try:
            shell32.SetCurrentProcessExplicitAppUserModelID("Rackhand.NetworkAssistant")
        except Exception:
            pass

        # Set console window title (and OSC title for Windows Terminal)
        try:
            kernel32.SetConsoleTitleW("Rackhand")
            sys.stdout.write("\x1b]0;Rackhand\x07")
            sys.stdout.flush()
        except Exception:
            pass

        kernel32.GetConsoleWindow.restype = wintypes.HWND
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        # Locate rackhand.ico (bundled or alongside exe/cwd)
        ico_candidates = [
            Path(__file__).resolve().parent / "rackhand.ico",
            Path(sys.executable).resolve().parent / "rackhand.ico",
            Path.cwd() / "rackhand.ico",
        ]
        ico_path = next((p for p in ico_candidates if p.is_file()), None)

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_ssize_t

        hicon_small = None
        hicon_big = None

        if ico_path:
            hicon_small = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            hicon_big = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)

        # Fallback: extract embedded icon from rackhand.exe if .ico file not found on disk
        if not hicon_big:
            try:
                from updater import find_current_executable
                exe_path = find_current_executable()
                if exe_path and exe_path.is_file():
                    shell32.ExtractIconExW.argtypes = [
                        wintypes.LPCWSTR, ctypes.c_int,
                        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT,
                    ]
                    shell32.ExtractIconExW.restype = wintypes.UINT
                    big_arr = (wintypes.HICON * 1)()
                    small_arr = (wintypes.HICON * 1)()
                    if shell32.ExtractIconExW(str(exe_path), 0, big_arr, small_arr, 1) > 0:
                        hicon_big = big_arr[0] or hicon_big
                        hicon_small = small_arr[0] or hicon_small
            except Exception:
                pass

        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
    except Exception:
        pass


def main() -> None:
    _configure_windows_console_icon()
    backend = RealBackend()
    app = RackhandApp(backend=backend)
    app.run()


if __name__ == "__main__":
    main()
