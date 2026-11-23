"""Unit tests for /fork session cloning."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from models import ApprovalDecision, DeviceProfile, DeviceSession, ProposedChange, ProviderConfig, Risk, Settings, Transport, Vendor
from llm_engine import ContextMemoryManager
from ui.messages import AssistantMessage, CompactionCheckpointMessage, GenericMessage, ProposalVerdictMessage, ToolBundleMessage, ToolCallEntry, UserMessage
from ui.suggestions import get_suggestions


def test_context_memory_manager_clone():
    mem = ContextMemoryManager()
    mem.add_message("sess-1", "user", "Configure BGP ASN 65000")
    mem.add_message("sess-1", "assistant", "Checking current BGP configuration...")

    mem.clone_session("sess-1", "sess-1-fork")

    # Cloned messages must match
    fork_msgs = mem.get_messages("sess-1-fork")
    assert len(fork_msgs) == 2
    assert fork_msgs[0]["content"] == "Configure BGP ASN 65000"

    # Deepcopy check: modifying fork must not mutate original
    mem.add_message("sess-1-fork", "user", "Add neighbor 10.0.0.2")
    assert len(mem.get_messages("sess-1-fork")) == 3
    assert len(mem.get_messages("sess-1")) == 2


def test_chat_message_cloning():
    u = UserMessage("Show version")
    u_clone = u.clone()
    assert isinstance(u_clone, UserMessage)
    assert u_clone.text == u.text
    assert u_clone.widget is None

    a = AssistantMessage("Cisco IOS Software, C3750 Software")
    a_clone = a.clone()
    assert isinstance(a_clone, AssistantMessage)
    assert a_clone.markdown == a.markdown
    assert a_clone.widget is None

    c = CompactionCheckpointMessage("### Checkpoint Summary")
    c_clone = c.clone()
    assert isinstance(c_clone, CompactionCheckpointMessage)
    assert c_clone.summary == c.summary
    assert c_clone.widget is None

    t = ToolBundleMessage(calls=[ToolCallEntry("call_1", "sw1", "show vlan brief", Risk.READ, "VLAN 1 active", True, False)])
    t_clone = t.clone()
    assert isinstance(t_clone, ToolBundleMessage)
    assert len(t_clone.calls) == 1
    assert t_clone.calls[0].command == "show vlan brief"
    # Ensure call entry list is copied
    t_clone.calls.append(ToolCallEntry("call_2", "sw1", "show cdp", Risk.READ, "", True, False))
    assert len(t.calls) == 1
    assert len(t_clone.calls) == 2

    p = ProposalVerdictMessage(ProposedChange("chg_1", "sw1", "VLAN 20", ["vlan 20"], Risk.CONFIG), ApprovalDecision(True))
    p_clone = p.clone()
    assert isinstance(p_clone, ProposalVerdictMessage)
    assert p_clone.change.title == "VLAN 20"
    assert p_clone.decision.approved is True


def test_suggestions_include_fork():
    suggestions = get_suggestions("/fo")
    assert any(s.value == "/fork" for s in suggestions)
    full_suggestions = get_suggestions("/")
    assert any(s.value == "/fork" for s in full_suggestions)


def test_ui_workspace_fork_workflow():
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
        screen._mount_message = MagicMock()
        screen.notify = MagicMock()
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)

        # 1. Create root session with custom title
        sess1 = screen.create_chat_session()
        sess1.title = "Configure OSPF"
        screen.active_session_id = sess1.id

        u_msg = UserMessage("How do I set router-id?")
        a_msg = AssistantMessage("Use 'router-id 1.1.1.1' under 'router ospf 1'.")
        screen.chat_messages[sess1.id] = [u_msg, a_msg]

        # 2. Trigger /fork
        screen._handle_command("/fork")

        # Verify new session created
        assert len(screen.chat_sessions) == 2
        fork1 = screen.chat_sessions[1]
        assert fork1.title == "Configure OSPF (fork #1)"
        assert screen.active_session_id == fork1.id
        assert len(screen.chat_messages[fork1.id]) == 2
        assert screen.chat_messages[fork1.id][0].text == "How do I set router-id?"
        app.backend.clone_session_memory.assert_called_with(sess1.id, fork1.id)

        # 3. Trigger /fork again from the fork
        screen._handle_command("/fork")
        assert len(screen.chat_sessions) == 3
        fork2 = screen.chat_sessions[2]
        assert fork2.title == "Configure OSPF (fork #2)"
        assert screen.active_session_id == fork2.id

        # 4. Fork an untitled session ("Session 4")
        sess_default = screen.create_chat_session()
        screen.active_session_id = sess_default.id
        screen._handle_command("/fork")
        fork_default = screen.chat_sessions[-1]
        assert f"{sess_default.display_title} (fork #1)" == fork_default.title
    finally:
        active_app.reset(token)
