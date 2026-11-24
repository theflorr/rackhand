from __future__ import annotations

from unittest.mock import MagicMock
import pytest
from textual.app import App
from textual.events import Key
from textual._context import active_app

from models import (
    AgentToolResult,
    DeviceProfile,
    DeviceSession,
    ProviderConfig,
    Settings,
    Transport,
    Vendor,
)
from ui.messages import UserMessage
from ui.suggestions import PromptInput
from ui.screens.workspace import QueuedPrompt, WorkspaceScreen


@pytest.fixture
def dummy_app():
    class DummyApp(App):
        def clear_selection(self):
            pass

    app = DummyApp()
    app.sessions = []
    app.active = DeviceSession(
        id="dev-1",
        profile=DeviceProfile(name="cisco-edge", vendor=Vendor.CISCO_IOS, transport=Transport.SERIAL),
        prompt="cisco-edge#",
        hostname="cisco-edge",
    )
    app.provider = ProviderConfig(key="p1", name="prov", model="m1", api_key="k1")
    app.settings = Settings()
    app.backend = MagicMock()
    token = active_app.set(app)
    yield app
    active_app.reset(token)


def test_ctrl_enter_and_shift_enter_inserts_newline(dummy_app):
    inp = PromptInput()
    inp.value = "first line"
    inp.cursor_position = len(inp.value)

    # Press Shift+Enter
    ev_shift = Key("shift+enter", "shift+enter")
    inp.on_key(ev_shift)
    assert inp.value == "first line\n"
    assert ev_shift._no_default_action is True
    assert ev_shift._stop_propagation is True

    # Type second line
    inp._insert_token_or_text("second line")

    # Press Ctrl+Enter
    ev_ctrl = Key("ctrl+enter", "ctrl+enter")
    inp.on_key(ev_ctrl)
    assert inp.value == "first line\nsecond line\n"
    assert ev_ctrl._no_default_action is True


def test_multiline_height_and_cursor_navigation(dummy_app):
    inp = PromptInput()
    inp.value = "line 1\nline 2\nline 3"
    inp._sync_multiline_height()
    assert inp.styles.height.value == 3

    # Position cursor on line 2 (middle line)
    # "line 1\n" is 7 chars, so index 9 is 'n' in 'line 2'
    inp.cursor_position = 9
    inp._move_cursor_up()
    # Should move to line 1
    assert inp.cursor_position == 2  # 'n' in 'line 1'

    inp._move_cursor_down()
    assert inp.cursor_position == 9

    # Multiline render_line test
    strip0 = inp.render_line(0)
    assert strip0 is not None
    strip1 = inp.render_line(1)
    assert strip1 is not None
    strip2 = inp.render_line(2)
    assert strip2 is not None


def test_enter_queues_prompt_when_agent_busy(dummy_app):
    screen = WorkspaceScreen()
    screen._app = dummy_app
    screen.notify = MagicMock()
    screen._mount_message = MagicMock()
    screen._run_agent = MagicMock()
    screen._persist_session = MagicMock()
    mock_scroll = MagicMock()
    screen.query_one = MagicMock(return_value=mock_scroll)

    screen.create_chat_session()
    screen.active_session_id = screen.chat_sessions[0].id

    # Agent is currently busy running a prompt!
    screen._busy = True

    # User submits a second prompt
    screen._send("Second prompt while busy")

    # Should be queued, NOT executed immediately
    assert len(screen._prompt_queue) == 1
    queued = screen._prompt_queue[0]
    assert queued.display_text == "Second prompt while busy"
    screen._run_agent.assert_not_called()
    assert screen.notify.call_count >= 1


def test_queued_prompt_fires_after_agent_finishes(dummy_app):
    screen = WorkspaceScreen()
    screen._app = dummy_app
    screen.notify = MagicMock()
    screen._mount_message = MagicMock()
    screen._run_agent = MagicMock()
    screen._persist_session = MagicMock()
    mock_scroll = MagicMock()
    screen.query_one = MagicMock(return_value=mock_scroll)

    screen.create_chat_session()
    screen.active_session_id = screen.chat_sessions[0].id

    # Add a prompt to the queue
    screen._prompt_queue.append(QueuedPrompt(
        display_text="Queued follow-up",
        expanded_text="Queued follow-up",
        attached_images={},
    ))

    # Trigger turn completion
    screen._busy = True
    # Simulate finally block
    screen._set_busy(False)
    if screen._prompt_queue and not screen._busy:
        next_item = screen._prompt_queue.pop(0)
        screen._execute_prompt(next_item.display_text, next_item.expanded_text, next_item.attached_images)

    assert len(screen._prompt_queue) == 0
    assert screen._mount_message.call_count == 1
    assert screen._run_agent.call_count == 1
    screen._run_agent.assert_called_with("Queued follow-up")


def test_alt_m_sends_mid_turn_with_loading_symbol(dummy_app):
    screen = WorkspaceScreen()
    screen._app = dummy_app
    screen.notify = MagicMock()
    screen._mount_message = MagicMock()
    screen._run_agent = MagicMock()
    screen._persist_session = MagicMock()
    screen.set_interval = MagicMock(return_value=MagicMock())
    mock_scroll = MagicMock()
    screen.query_one = MagicMock(return_value=mock_scroll)

    screen.create_chat_session()
    screen.active_session_id = screen.chat_sessions[0].id

    # Agent is busy
    screen._busy = True

    # Queue a prompt
    screen._prompt_queue.append(QueuedPrompt(
        display_text="Check Gi0/1 errors mid-turn",
        expanded_text="Check Gi0/1 errors mid-turn",
        attached_images={},
    ))

    # User presses Alt+M
    screen.action_send_mid_turn()

    # 1. Mounts UserMessage with live loading status
    assert screen._mount_message.call_count == 1
    mounted = screen._mount_message.call_args[0][0]
    assert isinstance(mounted, UserMessage)
    assert mounted.status == "sending"
    assert "Sending to agent..." in mounted.render().plain

    # 2. Injected into backend
    dummy_app.backend.inject_mid_turn_prompt.assert_called_with(
        screen.active_session_id,
        "Check Gi0/1 errors mid-turn",
    )

    # 3. Spinner is started
    assert screen._spinner_timer is not None

    # Simulate tool call completion (AgentToolResult arrives)
    ev_result = AgentToolResult("call-1", "Gi0/1 is UP, 0 errors", True)
    if screen._pending_mid_turn_msg:
        screen._pending_mid_turn_msg.status = "sent"
        screen._pending_mid_turn_msg.update_widget = MagicMock()
        screen._pending_mid_turn_msg = None
        screen._stop_spinner()

    assert mounted.status == "sent"
    assert "✓ Sent to agent" in mounted.render().plain


def test_user_message_sending_and_sent_rendering():
    # Sending status with braille spinner
    msg = UserMessage("Run diagnostics", status="sending")
    rendered = msg.render()
    assert "Sending to agent..." in rendered.plain
    assert "⠋" in rendered.plain

    # Sent status
    msg.status = "sent"
    rendered_sent = msg.render()
    assert "✓ Sent to agent" in rendered_sent.plain


def test_build_llm_request_import_and_execution():
    from llm_engine import build_llm_request
    from models import ApiScheme

    # 1. Standard OpenAI / OpenRouter provider
    prov_openai = ProviderConfig(
        key="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test1234",
        model="meta-llama/llama-3.3-70b-instruct",
    )
    endpoint, headers, payload, is_anthropic = build_llm_request(
        prov_openai, [{"role": "user", "content": "hi"}], tools=[], max_tokens=5
    )
    assert endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer sk-or-test1234"
    assert payload["max_tokens"] == 5
    assert is_anthropic is False

    # 2. Anthropic provider
    prov_anthropic = ProviderConfig(
        key="anthropic",
        name="Anthropic",
        scheme=ApiScheme.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test1234",
        model="claude-3-5-sonnet-20241022",
    )
    endpoint, headers, payload, is_anthropic = build_llm_request(
        prov_anthropic, [{"role": "user", "content": "hi"}], tools=[], max_tokens=10
    )
    assert endpoint == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test1234"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["max_tokens"] == 10
    assert is_anthropic is True


def test_queue_multiple_commands_sequential_execution(dummy_app):
    screen = WorkspaceScreen()
    screen._app = dummy_app
    screen.notify = MagicMock()
    screen._mount_message = MagicMock()
    screen._run_agent = MagicMock()
    screen._persist_session = MagicMock()
    mock_scroll = MagicMock()
    screen.query_one = MagicMock(return_value=mock_scroll)

    screen.create_chat_session()
    screen.active_session_id = screen.chat_sessions[0].id

    # Agent is busy
    screen._busy = True

    # User queues 3 commands in order
    screen._send("command 1")
    screen._send("command 2")
    screen._send("command 3")

    assert len(screen._prompt_queue) == 3
    assert [q.display_text for q in screen._prompt_queue] == ["command 1", "command 2", "command 3"]

    executed_prompts = []
    screen._execute_prompt = lambda d, e, a: executed_prompts.append(d)

    # First turn finishes -> dispatches next
    screen._busy = False
    screen._dispatch_next_queued()
    assert executed_prompts == ["command 1"]
    assert len(screen._prompt_queue) == 2

    # Second turn finishes -> dispatches next
    screen._dispatch_next_queued()
    assert executed_prompts == ["command 1", "command 2"]
    assert len(screen._prompt_queue) == 1

    # Third turn finishes -> dispatches next
    screen._dispatch_next_queued()
    assert executed_prompts == ["command 1", "command 2", "command 3"]
    assert len(screen._prompt_queue) == 0


def test_queue_ui_shows_messages_above_prompt_and_alt_m_hint(dummy_app):
    from textual.widgets import Static
    from textual.containers import Horizontal

    screen = WorkspaceScreen()
    screen._app = dummy_app

    mock_queue_bar = Horizontal(id="composer-queue-bar")
    mock_queue_content = Static(id="composer-queue-content")
    mock_queue_hint = Static(id="composer-queue-hint")
    mock_status = Static(id="composer-status")

    def mock_query(selector, expected_type=None):
        if selector == "#composer-queue-bar":
            return mock_queue_bar
        if selector == "#composer-queue-content":
            return mock_queue_content
        if selector == "#composer-queue-hint":
            return mock_queue_hint
        if selector == "#composer-status":
            return mock_status
        return MagicMock()

    screen.query_one = mock_query

    # Initially empty
    screen._update_queue_ui()
    assert mock_queue_bar.display is False

    # Queue 2 commands
    screen._prompt_queue.append(QueuedPrompt(display_text="show ip int br", expanded_text="show ip int br", attached_images={}))
    screen._prompt_queue.append(QueuedPrompt(display_text="show version", expanded_text="show version", attached_images={}))

    screen._update_queue_ui()
    assert mock_queue_bar.display is True
    assert "-visible" in mock_queue_bar.classes
    rendered_content = str(mock_queue_content._render())
    assert "show ip int br" in rendered_content
    assert "show version" in rendered_content
    assert "\n" in rendered_content  # Under each other vertically
    assert "⏳" not in rendered_content  # Emoji removed
    assert "◈" in rendered_content  # Custom symbol present
    assert "Press" in str(mock_queue_hint._render()) and "ALT+M" in str(mock_queue_hint._render())

    # Clear queue
    screen._prompt_queue.clear()
    screen._update_queue_ui()
    assert mock_queue_bar.display is False
    assert "-visible" not in mock_queue_bar.classes

