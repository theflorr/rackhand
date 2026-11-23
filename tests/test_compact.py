"""Unit tests for /compact conversation context compaction."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models import ApiScheme, DeviceProfile, DeviceSession, ProviderConfig, Settings, Transport, Vendor
from agents import get_agent
from llm_engine import ContextMemoryManager, RealLLMEngine
from ui.messages import CompactionCheckpointMessage, UserMessage, AssistantMessage
from ui.suggestions import get_suggestions


def test_context_memory_manager_clear():
    mem = ContextMemoryManager()
    mem.add_message("sess-1", "user", "Hello")
    mem.add_message("sess-1", "assistant", "Hi there")
    mem.add_message("sess-2", "user", "Other session")

    assert len(mem.get_messages("sess-1")) == 2
    assert len(mem.get_messages("sess-2")) == 1

    mem.clear("sess-1")
    assert len(mem.get_messages("sess-1")) == 0
    # Other sessions remain untouched
    assert len(mem.get_messages("sess-2")) == 1


def test_format_messages_for_compaction():
    messages = [
        {"role": "system", "content": "You are a network engineer."},
        {"role": "user", "content": "Check VLAN 10 status."},
        {
            "role": "assistant",
            "content": "Checking VLAN now.",
            "tool_calls": [{
                "function": {
                    "name": "run_command",
                    "arguments": '{"command": "show vlan id 10"}',
                }
            }]
        },
        {"role": "tool", "name": "run_command", "content": "VLAN 10 is active on Fa0/1"},
        {"role": "assistant", "content": "VLAN 10 is active on port FastEthernet0/1."},
    ]

    engine = RealLLMEngine()
    formatted = engine._format_messages_for_compaction(messages)

    assert "USER: Check VLAN 10 status." in formatted
    assert "run_command" in formatted
    assert "show vlan id 10" in formatted
    assert "VLAN 10 is active on Fa0/1" in formatted
    assert "VLAN 10 is active on port FastEthernet0/1." in formatted
    # System message must not be duplicated into user transcript
    assert "You are a network engineer." not in formatted


def test_compact_session_flow():
    async def _test():
        engine = RealLLMEngine()
        session_id = "test-session-compact"

        # Seed existing conversation
        engine.memory.add_message(session_id, "system", "Base system prompt")
        engine.memory.add_message(session_id, "user", "Help configure OSPF on GigabitEthernet0/0")
        engine.memory.add_message(session_id, "assistant", "I will run show ip interface brief")
        engine.memory.add_message(session_id, "tool", "GigabitEthernet0/0 is 10.0.0.1/24 up/up", name="run_command")
        assert len(engine.memory.get_messages(session_id)) == 4

        dev_session = DeviceSession(
            id="dev1",
            profile=DeviceProfile(name="cisco-core", vendor=Vendor.CISCO_IOS, transport=Transport.SSH),
            hostname="Core-Router-1",
        )
        prov = ProviderConfig(key="test-prov", spec_id="test", name="Test Provider", api_key="secret", base_url="https://api.test/v1")
        agent_spec = get_agent("cisco-ios")

        mock_summary = (
            "### Session Objective & Scope\n"
            "- Configure OSPF on GigabitEthernet0/0.\n\n"
            "### Connected Devices & Discovered State\n"
            "- Target: Core-Router-1 (Cisco IOS), Gi0/0 is 10.0.0.1/24.\n\n"
            "### Actions Performed & Diagnostic Findings\n"
            "- Executed `show ip interface brief` confirming Gi0/0 is up/up.\n\n"
            "### Configuration & Architectural Decisions\n"
            "- OSPF Area 0 planned for 10.0.0.0/24 subnet.\n\n"
            "### Current Status & Remaining Next Steps\n"
            "- Ready to generate OSPF network statement."
        )

        with patch.object(engine, "call_llm_stateless", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mock_summary

            summary_result = await engine.compact_session(
                session_id=session_id,
                provider=prov,
                session=dev_session,
                agent_spec=agent_spec,
                is_plan_mode=False,
            )

            assert summary_result == mock_summary
            mock_call.assert_awaited_once()

            # Check that memory was cleared and re-seeded
            new_history = engine.memory.get_messages(session_id)
            assert len(new_history) == 3

            # 1. System prompt
            assert new_history[0]["role"] == "system"
            assert "Core-Router-1" in new_history[0]["content"]

            # 2. Checkpoint user prompt
            assert new_history[1]["role"] == "user"
            assert "[Session Compaction Checkpoint" in new_history[1]["content"]
            assert mock_summary in new_history[1]["content"]

            # 3. Checkpoint assistant ack
            assert new_history[2]["role"] == "assistant"
            assert "loaded the conversation checkpoint" in new_history[2]["content"]

        await engine.aclose()

    asyncio.run(_test())


def test_suggestions_include_compact():
    suggestions = get_suggestions("/com")
    assert any(s.value == "/compact" for s in suggestions)
    full_suggestions = get_suggestions("/")
    assert any(s.value == "/compact" for s in full_suggestions)


def test_compaction_checkpoint_message_rendering():
    summary_text = "### Session Objective & Scope\n- Debug BGP flapping"
    msg = CompactionCheckpointMessage(summary=summary_text, tokens_compacted=1200)
    panel = msg.render()
    assert panel.title == "checkpoint: session compacted"
    assert "Prior turns compacted" in str(panel.subtitle)


def test_ui_workspace_compact_command():
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
            self.backend.compact_session = AsyncMock(return_value="### Checkpoint\n- Compacted successfully")
            self.backend.get_session_messages = MagicMock(return_value=[])

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen._bubble = MagicMock()
        screen._mount_message = MagicMock()
        screen.notify = MagicMock()

        sess = screen.create_chat_session()
        screen.active_session_id = sess.id

        # 1. Test empty session: compact should notify/bubble warning and not call backend
        screen._handle_command("/compact")
        assert screen._bubble.call_count == 1
        app.backend.compact_session.assert_not_called()

        # 2. Add messages to session
        u_msg = UserMessage("How do I configure VLAN 10?")
        a_msg = AssistantMessage("Use 'vlan 10' in config mode.")
        screen.chat_messages[sess.id] = [u_msg, a_msg]

        # Mock query_one for textual widgets
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        # 3. Test _run_compact execution
        async def run_compact_test():
            await screen._run_compact(sess.id)

        asyncio.run(run_compact_test())

        app.backend.compact_session.assert_awaited_once()
        mock_scroll.remove_children.assert_called_once()
        # Verify checkpoint message was mounted
        assert screen._mount_message.call_count >= 1
        mounted_msg = screen._mount_message.call_args[0][0]
        assert isinstance(mounted_msg, CompactionCheckpointMessage)
        assert "Compacted successfully" in mounted_msg.summary
    finally:
        active_app.reset(token)

