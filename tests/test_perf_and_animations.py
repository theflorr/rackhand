"""Tests for performance optimizations, token stream throttling, and UI animations."""

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import ChatSession, ProviderConfig, Settings
from ui.clipboard import _ensure_pil, clear_test_clipboard, set_test_clipboard
from ui.messages import AssistantMessage, ToolBundleMessage, ToolCallEntry
from ui.screens.workspace import WorkspaceScreen


def test_deferred_pil_import():
    # Calling _ensure_pil returns (Image, ImageGrab) when PIL is installed
    img_cls, grab_cls = _ensure_pil()
    assert img_cls is not None or "PIL" not in sys.modules


@pytest.mark.asyncio
async def test_streaming_throttle_buffering():
    from textual.app import App
    from textual._context import active_app
    from models import AgentText

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.active_id = None
            self.provider = ProviderConfig(key="test", name="Test", model="test-model")
            self.settings = Settings()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen.active_session_id = "test-session"
        screen.chat_messages["test-session"] = []
        screen._mount_message = MagicMock()
        screen._scroll = MagicMock()
        screen.update_bars = MagicMock()

        # Simulate stream of delta tokens
        deltas = ["Hello", " ", "world", "!", " How", " are", " you", " today?"]

        async def fake_run_turn(*args, **kwargs):
            for d in deltas:
                yield AgentText(delta=d)

        mock_backend = MagicMock()
        mock_backend.run_turn = fake_run_turn
        app.backend = mock_backend

        # Run agent directly via wrapped coroutine
        await screen._run_agent.__wrapped__(screen, "Hi")

        # Assistant message was mounted and contains full text
        assert screen._mount_message.called
        mounted = screen._mount_message.call_args[0][0]
        assert isinstance(mounted, AssistantMessage)
        assert mounted.markdown == "".join(deltas)
    finally:
        active_app.reset(token)


@pytest.mark.asyncio
async def test_thinking_spinner_ticks_while_busy():
    screen = WorkspaceScreen()
    screen._app = MagicMock()
    screen.query_one = MagicMock()
    screen.update_bars = MagicMock()

    assert screen._busy is False
    assert screen._spinner_timer is None

    # Simulate agent becoming busy
    screen._set_busy(True)
    assert screen._busy is True
    assert screen._spinner_timer is not None

    init_idx = screen._spinner_idx
    screen._tick_spinner()
    assert screen._spinner_idx == (init_idx + 1) % len(screen.SPINNER_FRAMES)
    assert screen.update_bars.called

    # When no longer busy, spinner stops
    screen._set_busy(False)
    assert screen._busy is False
    assert screen._spinner_timer is None


@pytest.mark.asyncio
async def test_tabs_styling_equal_and_no_block_highlight():
    from textual.app import App
    from textual.widgets import Input, TabbedContent, TabPane
    from textual.widgets._tabbed_content import ContentTabs, ContentTab
    from ui.styles import APP_CSS

    class TabTestApp(App):
        CSS = APP_CSS

        def compose(self):
            with TabbedContent(id="main", initial="tab-agent"):
                with TabPane("agent", id="tab-agent"):
                    yield Input(id="prompt")
                with TabPane("console", id="tab-console"):
                    yield Input(id="console-input")
                with TabPane("settings", id="tab-settings"):
                    yield Input(id="setting-input")

    app = TabTestApp()
    async with app.run_test() as pilot:
        tabs = app.query_one(ContentTabs)

        # 1. Switch to settings tab
        await pilot.click("#--content-tab-tab-settings")
        await pilot.pause()
        assert tabs.active_tab.id == "--content-tab-tab-settings"
        # Background must remain transparent (not highlighted with block cursor)
        assert tabs.active_tab.styles.background.a == 0

        # 2. Switch to console tab
        await pilot.click("#--content-tab-tab-console")
        await pilot.pause()
        assert tabs.active_tab.id == "--content-tab-tab-console"
        assert tabs.active_tab.styles.background.a == 0

        # 3. Switch to agent tab
        await pilot.click("#--content-tab-tab-agent")
        await pilot.pause()
        assert tabs.active_tab.id == "--content-tab-tab-agent"
        assert tabs.active_tab.styles.background.a == 0

        # 4. Checkbox focus has no block highlight
        chk = app.query_one("#setting-input")
        from ui.styles import _rackhand_theme
        theme = _rackhand_theme()
        assert theme.variables.get("block-cursor-background") == "transparent"
        assert theme.variables.get("block-hover-background") == "transparent"

