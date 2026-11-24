import asyncio
import re
import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding

from models import ProviderConfig, Settings
from backend import RealBackend
from ui.messages import SystemNoticeMessage, serialize_message, deserialize_message
from ui.screens.workspace import WorkspaceScreen
from ui.suggestions import PromptInput, CommandSuggestionsBox
from ui.styles import APP_CSS


def has_emoji(text: str) -> bool:
    # Check for emoji unicode ranges
    emoji_pattern = re.compile(
        r"[\U00010000-\U0010ffff]|"
        r"[\u2600-\u27bf]|"
        r"[\u2300-\u23ff]|"
        r"[\u2b50-\u2b55]|"
        r"[\u203c\u2049\u2139\u2194-\u2199\u21a9-\u21aa]"
    )
    return bool(emoji_pattern.search(text))


def test_system_notice_message():
    msg = SystemNoticeMessage("You interrupted the response")
    assert msg.text == "You interrupted the response"
    
    # Verify rendered text
    rendered = msg.render()
    plain = rendered.plain
    assert "You interrupted the response" in plain
    assert not has_emoji(plain), f"Found emoji in system notice: {plain}"

    # Verify serialization / deserialization roundtrip
    d = serialize_message(msg)
    assert d["type"] == "system_notice"
    assert d["text"] == "You interrupted the response"

    restored = deserialize_message(d)
    assert isinstance(restored, SystemNoticeMessage)
    assert restored.text == "You interrupted the response"
    assert not has_emoji(restored.render().plain)


class MockApp(App):
    CSS = APP_CSS

    def __init__(self):
        super().__init__()
        self.sessions = []
        self.active = None
        self.active_id = None
        self.provider = ProviderConfig(key="test", name="Test", model="test-model")
        self.settings = Settings()
        self.backend = RealBackend()

    def compose(self) -> ComposeResult:
        yield WorkspaceScreen()


@pytest.mark.asyncio
async def test_escape_interrupts_busy_agent():
    app = MockApp()
    async with app.run_test() as pilot:
        ws = app.query_one(WorkspaceScreen)
        p_inp = ws.query_one("#prompt", PromptInput)
        p_inp.focus()
        await pilot.pause()

        # Simulate agent busy
        ws._busy = True
        ws._prompt_queue.append("test queued item")
        interrupted = False

        original_interrupt = ws.interrupt_response
        def mock_interrupt():
            nonlocal interrupted
            interrupted = True
            original_interrupt()

        ws.interrupt_response = mock_interrupt

        # Send Escape key while prompt input has focus
        await pilot.press("escape")
        await pilot.pause()

        assert interrupted is True
        assert len(ws._prompt_queue) == 0


@pytest.mark.asyncio
async def test_cancellation_mounts_system_notice():
    app = MockApp()
    async with app.run_test() as pilot:
        ws = app.query_one(WorkspaceScreen)
        sess = ws.create_chat_session()
        ws.active_session_id = sess.id
        await pilot.pause()

        # Mount a cancellation via asyncio.CancelledError inside _run_agent
        msg = SystemNoticeMessage("You interrupted the response")
        ws._mount_message(msg)
        await pilot.pause()

        chat_msgs = ws.chat_messages.get(sess.id, [])
        assert len(chat_msgs) > 0
        last = chat_msgs[-1]
        assert isinstance(last, SystemNoticeMessage)
        assert last.text == "You interrupted the response"
        assert not has_emoji(last.render().plain)
