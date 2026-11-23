"""Unit tests for /rename session renaming and visual sidebar elements."""

import pytest
from unittest.mock import MagicMock

from models import DeviceProfile, DeviceSession, ProviderConfig, Settings, Transport, Vendor
from ui.screens.workspace import SessionRow
from ui.suggestions import get_suggestions


def test_suggestions_rename():
    ren_subs = get_suggestions("/ren")
    assert any(s.value == "/rename" for s in ren_subs)

    rem_subs = get_suggestions("/rem")
    assert not any(s.value == "/remake" for s in rem_subs)


def test_session_row_active_indicator():
    from textual.app import App
    from textual._context import active_app

    app = App()
    token = active_app.set(app)
    try:
        row_active = SessionRow("sess-1", "VLAN Setup", is_active=True)
        widgets_active = list(row_active.compose())
        static_active = widgets_active[0]
        rendered_active = static_active.render()
        # Active session uses bold white style without extra complex symbols
        assert "▸" not in str(rendered_active)
        assert "VLAN Setup" in str(rendered_active)

        row_inactive = SessionRow("sess-2", "BGP Setup", is_active=False)
        widgets_inactive = list(row_inactive.compose())
        static_inactive = widgets_inactive[0]
        rendered_inactive = static_inactive.render()
        assert "BGP Setup" in str(rendered_inactive)
    finally:
        active_app.reset(token)


def test_ui_workspace_rename_workflow():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.active_id = None
            self.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen._bubble = MagicMock()
        screen.notify = MagicMock()
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        sess = screen.create_chat_session()
        screen.active_session_id = sess.id
        assert sess.title is None

        # 1. Test /rename with new title
        screen._handle_command("/rename Core Switch Migration")
        assert sess.title == "Core Switch Migration"
        assert sess.has_prompt is True
        screen.notify.assert_called_with("Session renamed to: Core Switch Migration", title="Session Renamed")

        # 2. Test empty argument -> shows usage guide without altering title
        screen._handle_command("/rename")
        assert sess.title == "Core Switch Migration"
        assert screen._bubble.call_count >= 1

        # 3. Test removed /remake command is treated as unknown
        screen._handle_command("/remake Another Title")
        assert sess.title == "Core Switch Migration"
    finally:
        active_app.reset(token)


def test_refresh_device_list_visual_elements():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            dev1 = DeviceSession(
                id="dev1",
                profile=DeviceProfile(name="cisco-core", vendor=Vendor.CISCO_IOS, transport=Transport.SSH),
                hostname="Core-Router-1",
            )
            dev2 = DeviceSession(
                id="dev2",
                profile=DeviceProfile(name="fgt-edge", vendor=Vendor.FORTIOS, transport=Transport.SERIAL),
                hostname="FGT-FW-1",
            )
            self.sessions = [dev1, dev2]
            self.active = dev1
            self.active_id = "dev1"
            self.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        mock_list = MagicMock()
        screen.query_one = MagicMock(return_value=mock_list)

        screen.refresh_device_list()
        mock_list.clear_options.assert_called_once()
        assert mock_list.add_option.call_count == 2

        # Verify active device option text has clean device name and transport without geometric symbols
        first_opt = mock_list.add_option.call_args_list[0][0][0]
        assert "●" not in str(first_opt.prompt)
        assert "Core-Router-1" in str(first_opt.prompt)
        assert "ssh" in str(first_opt.prompt).lower()

        # Verify inactive device has simple styling
        second_opt = mock_list.add_option.call_args_list[1][0][0]
        assert "○" not in str(second_opt.prompt)
        assert "FGT-FW-1" in str(second_opt.prompt)
        assert "serial" in str(second_opt.prompt).lower()
    finally:
        active_app.reset(token)


def test_command_suggestions_box_no_wrapping():
    from unittest.mock import PropertyMock, patch
    from textual.geometry import Size
    from ui.suggestions import CommandSuggestionsBox, CommandSuggestion
    from rich.console import Console

    box = CommandSuggestionsBox()
    long_desc = "Enter plan mode (device discovery, read-only inspection, task planning) with very long text that would otherwise wrap around"
    box.suggestions = [
        CommandSuggestion("/plan", "/plan", long_desc),
        CommandSuggestion("/compact", "/compact", "Short summary"),
    ]
    with patch.object(CommandSuggestionsBox, "size", new_callable=PropertyMock, return_value=Size(60, 10)):
        group = box.render()

    c = Console(width=60)
    with c.capture() as cap:
        c.print(group)
    rendered = cap.get()
    non_empty_lines = [l for l in rendered.splitlines() if l.strip()]
    # Header + 2 items + footer = exactly 4 lines (no wrapping across lines)
    assert len(non_empty_lines) == 4
    # Ensure ellipsis is used to clip the long description
    assert "…" in non_empty_lines[1]


def test_console_device_picker_options():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App
    from textual._context import active_app
    from textual.widgets import Select

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            dev1 = DeviceSession(
                id="dev1",
                profile=DeviceProfile(name="cisco-core-switch-01", vendor=Vendor.CISCO_IOS, transport=Transport.SSH),
                hostname="Core-Switch-Primary-01",
            )
            self.sessions = [dev1]
            self.active = dev1
            self.active_id = "dev1"
            self.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
            self.settings = Settings()
            self.backend = MagicMock()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        mock_sel = MagicMock(spec=Select)
        mock_sel._options = []
        screen.query_one = MagicMock(return_value=mock_sel)
        screen._update_console_header = MagicMock()

        screen.refresh_console_devices()
        mock_sel.set_options.assert_called_once()
        opts = mock_sel.set_options.call_args[0][0]
        assert len(opts) == 1
        label, val = opts[0]
        assert val == "dev1"
        assert "Core-Switch-Primary-01" in label
        assert "[SSH]" in label
    finally:
        active_app.reset(token)

