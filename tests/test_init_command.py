"""Tests for /init command (device baseline discovery & connectivity verification)."""

from unittest.mock import MagicMock
import pytest

from models import (
    DeviceProfile,
    DeviceSession,
    ProviderConfig,
    Settings,
    Transport,
    Vendor,
)
from ui.messages import AssistantMessage
from ui.suggestions import CommandSuggestionsBox, get_suggestions
from ui.screens.workspace import WorkspaceScreen


def test_init_command_suggestions():
    # 1. Typing '/' includes '/init'
    suggestions = get_suggestions("/")
    cmd_names = [s.value for s in suggestions]
    assert "/init" in cmd_names

    # 2. Typing '/ini' or '/init' filters directly to '/init'
    ini_suggestions = get_suggestions("/ini")
    assert len(ini_suggestions) == 1
    assert ini_suggestions[0].value == "/init"
    assert "baseline" in ini_suggestions[0].description.lower() or "config" in ini_suggestions[0].description.lower()


def test_init_command_without_device():
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen.notify = MagicMock()
        screen._bubble = MagicMock()
        screen._run_agent = MagicMock()
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        screen.create_chat_session()
        screen.active_session_id = screen.chat_sessions[0].id

        # Run /init without connected device
        screen._handle_command("/init")

        screen.notify.assert_called_with("Connect a device first (Ctrl+N).", severity="warning")
        assert screen._bubble.call_count == 1
        screen._run_agent.assert_not_called()
    finally:
        active_app.reset(token)


def test_init_command_with_connected_device():
    from textual.app import App
    from textual._context import active_app

    dev = DeviceSession(
        id="dev-1",
        profile=DeviceProfile(name="cisco-edge", vendor=Vendor.CISCO_IOS, transport=Transport.SERIAL),
        prompt="cisco-edge#",
        hostname="cisco-edge",
    )

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = [dev]
            self.active = dev
            self.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen.notify = MagicMock()
        screen._mount_message = MagicMock()
        screen._run_agent = MagicMock()
        screen.refresh_session_list = MagicMock()
        screen.update_bars = MagicMock()
        screen._persist_session = MagicMock()
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        sess = screen.create_chat_session()
        screen.active_session_id = sess.id
        assert sess.title is None

        # Execute /init
        screen._handle_command("/init")

        # 1. Mounts initialization banner
        assert screen._mount_message.call_count == 1
        mounted_msg = screen._mount_message.call_args[0][0]
        assert isinstance(mounted_msg, AssistantMessage)
        assert "Device Initialization & Baseline Discovery" in mounted_msg.markdown
        assert "cisco-edge" in mounted_msg.markdown

        # 2. Session title is assigned automatically
        assert sess.title == "Init: cisco-edge"

        # 3. Triggers discovery agent turn
        assert screen._run_agent.call_count == 1
        prompt = screen._run_agent.call_args[0][0]
        assert "initialization" in prompt.lower()
        assert "show version" in prompt.lower() or "chassis/model" in prompt.lower()
        assert "routing" in prompt.lower()
    finally:
        active_app.reset(token)


def test_init_help_menu_entry():
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.provider = None
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen._bubble = MagicMock()
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        screen._handle_command("/help")

        assert screen._bubble.call_count == 1
        panel = screen._bubble.call_args[0][0]
        rendered_text = str(panel.renderable)
        assert "/init" in rendered_text
    finally:
        active_app.reset(token)
