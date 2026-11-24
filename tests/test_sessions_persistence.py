"""Unit and integration tests for session persistence (save/load) across Ubuntu and Windows."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import RealBackend
from models import (
    ApprovalDecision,
    ChatSession,
    ProposedChange,
    Risk,
    Settings,
)
from ui.messages import (
    AssistantMessage,
    CompactionCheckpointMessage,
    GenericMessage,
    ProposalVerdictMessage,
    ToolBundleMessage,
    ToolCallEntry,
    UserMessage,
    deserialize_message,
    serialize_message,
)


def test_chat_session_dict_roundtrip():
    sess = ChatSession(
        id="a1b2c3d4",
        num=3,
        title="BGP Peering Setup",
        has_prompt=True,
        tokens_in=4500,
        tokens_out=890,
        mode="plan",
        agent_id="cisco-nxos",
    )
    d = sess.to_dict()
    assert d["id"] == "a1b2c3d4"
    assert d["num"] == 3
    assert d["title"] == "BGP Peering Setup"
    assert d["has_prompt"] is True
    assert d["tokens_in"] == 4500
    assert d["tokens_out"] == 890
    assert d["mode"] == "plan"
    assert d["agent_id"] == "cisco-nxos"

    loaded = ChatSession.from_dict(d)
    assert loaded.id == sess.id
    assert loaded.num == sess.num
    assert loaded.title == sess.title
    assert loaded.has_prompt == sess.has_prompt
    assert loaded.tokens_in == sess.tokens_in
    assert loaded.tokens_out == sess.tokens_out
    assert loaded.mode == sess.mode
    assert loaded.agent_id == sess.agent_id
    assert loaded.display_title == "BGP Peering Setup"


def test_messages_serialization_roundtrip():
    # 1. UserMessage
    u = UserMessage("show ip route 10.0.0.0/8")
    u_d = serialize_message(u)
    assert u_d["type"] == "user"
    assert u_d["text"] == "show ip route 10.0.0.0/8"
    u_loaded = deserialize_message(u_d)
    assert isinstance(u_loaded, UserMessage)
    assert u_loaded.text == u.text

    # 2. AssistantMessage
    a = AssistantMessage("Routing table entry for 10.0.0.0/8 via 192.168.1.1")
    a_d = serialize_message(a)
    assert a_d["type"] == "assistant"
    assert "10.0.0.0/8" in a_d["markdown"]
    a_loaded = deserialize_message(a_d)
    assert isinstance(a_loaded, AssistantMessage)
    assert a_loaded.markdown == a.markdown

    # 3. CompactionCheckpointMessage
    c = CompactionCheckpointMessage("### Checkpoint\n- Configured Loopback0", tokens_compacted=3400)
    c_d = serialize_message(c)
    assert c_d["type"] == "checkpoint"
    assert c_d["tokens_compacted"] == 3400
    c_loaded = deserialize_message(c_d)
    assert isinstance(c_loaded, CompactionCheckpointMessage)
    assert c_loaded.summary == c.summary
    assert c_loaded.tokens_compacted == 3400

    # 4. ToolBundleMessage
    entry1 = ToolCallEntry(
        call_id="call-1",
        device="sw1",
        command="show version",
        risk=Risk.READ,
        output="Cisco IOS XE Version 17.3.3",
        ok=True,
        running=False,
    )
    entry2 = ToolCallEntry(
        call_id="call-2",
        device="sw1",
        command="show ip int br",
        risk=Risk.READ,
        output="GigabitEthernet1 up up",
        ok=True,
        running=False,
    )
    t = ToolBundleMessage(calls=[entry1, entry2], expanded=True)
    t_d = serialize_message(t)
    assert t_d["type"] == "tool_bundle"
    assert t_d["expanded"] is True
    assert len(t_d["calls"]) == 2
    t_loaded = deserialize_message(t_d)
    assert isinstance(t_loaded, ToolBundleMessage)
    assert t_loaded.expanded is True
    assert len(t_loaded.calls) == 2
    assert t_loaded.calls[0].command == "show version"
    assert t_loaded.calls[1].command == "show ip int br"
    assert t_loaded.calls[0].running is False

    # 5. ProposalVerdictMessage
    change = ProposedChange(
        id="chg-1",
        device="sw1",
        title="Add VLAN 100",
        commands=["vlan 100", "name USERS"],
        risk=Risk.CONFIG,
        rationale="Segregate user traffic",
        rollback=["no vlan 100"],
    )
    decision = ApprovalDecision(
        approved=True,
        commands=["vlan 100", "name USERS"],
        save_after=True,
        note="Approved by admin",
    )
    p = ProposalVerdictMessage(change, decision)
    p_d = serialize_message(p)
    assert p_d["type"] == "proposal_verdict"
    assert p_d["change"]["title"] == "Add VLAN 100"
    assert p_d["decision"]["approved"] is True
    p_loaded = deserialize_message(p_d)
    assert isinstance(p_loaded, ProposalVerdictMessage)
    assert p_loaded.change.title == change.title
    assert p_loaded.decision.approved is True
    assert p_loaded.decision.note == "Approved by admin"

    # 6. GenericMessage
    g = GenericMessage("Custom notice", css_class="info")
    g_d = serialize_message(g)
    assert g_d["type"] == "generic"
    assert g_d["content"] == "Custom notice"
    g_loaded = deserialize_message(g_d)
    assert isinstance(g_loaded, GenericMessage)
    assert g_loaded.content == "Custom notice"


@pytest.mark.asyncio
async def test_real_backend_session_storage(tmp_path: Path):
    backend = RealBackend()
    backend._dir = tmp_path
    backend._sessions_index_file = tmp_path / "sessions.json"
    backend._sessions_dir = tmp_path / "sessions"
    backend._sessions_dir.mkdir(parents=True, exist_ok=True)

    # 1. Initially no sessions
    sessions, active_id = await backend.load_chat_sessions()
    assert sessions == []
    assert active_id is None

    # 2. Save session 1
    sess1 = ChatSession(id="sess-01", num=1, title="Initial Switch Config", mode="build")
    ui_msgs1 = [
        UserMessage("Configure interface G0/1"),
        AssistantMessage("Interface GigabitEthernet0/1 configured."),
    ]
    mem1 = [
        {"role": "system", "content": "You are a network assistant"},
        {"role": "user", "content": "Configure interface G0/1"},
        {"role": "assistant", "content": "Interface GigabitEthernet0/1 configured."},
    ]
    await backend.save_chat_session(sess1, ui_msgs1, mem1, is_active=True)
    await backend.save_session_index([sess1], active_session_id=sess1.id)

    # Verify session index and file on disk
    assert backend._sessions_index_file.exists()
    s1_file = backend._sessions_dir / "sess-01.json"
    assert s1_file.exists()

    # 3. Load session 1 from disk
    loaded_sessions, cur_active = await backend.load_chat_sessions()
    assert len(loaded_sessions) == 1
    assert loaded_sessions[0].id == "sess-01"
    assert loaded_sessions[0].title == "Initial Switch Config"
    assert cur_active == "sess-01"

    # Load session 1 details (messages & memory)
    loaded_ui, loaded_mem = await backend.load_chat_session_details("sess-01")
    assert len(loaded_ui) == 2
    assert isinstance(loaded_ui[0], UserMessage)
    assert loaded_ui[0].text == "Configure interface G0/1"
    assert isinstance(loaded_ui[1], AssistantMessage)
    assert len(loaded_mem) == 3
    # Verify memory was pre-seeded in engine
    assert len(backend.get_session_messages("sess-01")) == 3

    # 4. Save session 2
    sess2 = ChatSession(id="sess-02", num=2, title="OSPF Troubleshooting", mode="plan")
    ui_msgs2 = [UserMessage("Check OSPF neighbors")]
    mem2 = [{"role": "user", "content": "Check OSPF neighbors"}]
    await backend.save_chat_session(sess2, ui_msgs2, mem2, is_active=True)
    await backend.save_session_index([sess1, sess2], active_session_id=sess2.id)

    loaded_sessions, cur_active = await backend.load_chat_sessions()
    assert len(loaded_sessions) == 2
    assert cur_active == "sess-02"

    # 5. Delete session 1
    await backend.delete_chat_session_persisted("sess-01")
    await backend.save_session_index([sess2], active_session_id=sess2.id)
    assert not s1_file.exists()

    loaded_sessions, cur_active = await backend.load_chat_sessions()
    assert len(loaded_sessions) == 1
    assert loaded_sessions[0].id == "sess-02"


@pytest.mark.asyncio
async def test_session_recovery_when_index_missing(tmp_path: Path):
    """If sessions.json index is missing or corrupt, discover and sort sessions from sessions/ directory."""
    backend = RealBackend()
    backend._dir = tmp_path
    backend._sessions_index_file = tmp_path / "sessions.json"
    backend._sessions_dir = tmp_path / "sessions"
    backend._sessions_dir.mkdir(parents=True, exist_ok=True)

    # Write 2 session files directly
    s1 = ChatSession(id="rec-01", num=1, title="First")
    s2 = ChatSession(id="rec-02", num=2, title="Second")

    await backend.save_chat_session(s2, [UserMessage("msg 2")], memory_messages=[])
    await backend.save_chat_session(s1, [UserMessage("msg 1")], memory_messages=[])

    # Ensure index does not exist
    if backend._sessions_index_file.exists():
        backend._sessions_index_file.unlink()

    # Load should discover and sort by num
    discovered, active_id = await backend.load_chat_sessions()
    assert len(discovered) == 2
    assert discovered[0].id == "rec-01"
    assert discovered[1].id == "rec-02"


@pytest.mark.asyncio
async def test_session_transcripts_persistence(tmp_path: Path):
    backend = RealBackend()
    transcripts_dir = tmp_path / "transcripts"
    settings = Settings(keep_transcripts=True, transcript_dir=str(transcripts_dir))

    sess = ChatSession(id="tr-01", num=1, title="VLAN Config: Switch 1 / Core")
    # Title has invalid path chars ': /' which should be safely sanitized for Windows & Ubuntu
    entry = "### [2026-11-24 10:00:00] USER\nshow vlan brief"

    await backend.append_session_transcript(sess, entry, settings)

    assert transcripts_dir.exists()
    transcript_files = list(transcripts_dir.glob("*.md"))
    assert len(transcript_files) == 1
    content = transcript_files[0].read_text(encoding="utf-8")
    assert "show vlan brief" in content

    # Test keep_transcripts=False disables writes
    settings_disabled = Settings(keep_transcripts=False, transcript_dir=str(transcripts_dir))
    await backend.append_session_transcript(sess, "### USER\nsecond command", settings_disabled)
    content2 = transcript_files[0].read_text(encoding="utf-8")
    assert "second command" not in content2


@pytest.mark.asyncio
async def test_workspace_restore_saved_sessions_flow():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App
    from textual._context import active_app

    sess1 = ChatSession(id="ws-1", num=1, title="Restored Session 1", mode="plan", agent_id="cisco-ios")
    sess2 = ChatSession(id="ws-2", num=2, title="Restored Session 2", mode="build", agent_id="fortios")

    mock_backend = MagicMock()
    mock_backend.load_chat_sessions = AsyncMock(return_value=([sess1, sess2], "ws-2"))
    mock_backend.load_chat_session_details = AsyncMock(return_value=(
        [UserMessage("Help with FortiGate")], [{"role": "user", "content": "Help with FortiGate"}]
    ))
    mock_backend.save_chat_session = AsyncMock()
    mock_backend.save_session_index = AsyncMock()

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.active_id = None
            self.backend = mock_backend
            self.settings = Settings()

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        mock_scroll = MagicMock()
        screen.query_one = MagicMock(return_value=mock_scroll)
        screen._welcome = MagicMock()
        screen.refresh_session_list = MagicMock()
        screen.refresh_device_list = MagicMock()
        screen.refresh_console_devices = MagicMock()
        screen._render_console = MagicMock()
        screen.update_bars = MagicMock()
        screen._load_settings_tab = MagicMock()
        screen._switch_to_session = MagicMock()

        await screen.on_mount()

        assert len(screen.chat_sessions) == 2
        assert screen.active_session_id == "ws-2"
        assert screen._session_counter == 2
        # Active session is eagerly restored on startup
        assert len(screen.chat_messages["ws-2"]) == 1
        screen._switch_to_session.assert_called_with("ws-2")

        # Inactive session ws-1 is loaded lazily on demand
        await screen._async_load_and_render_session("ws-1")
        assert len(screen.chat_messages["ws-1"]) == 1
    finally:
        active_app.reset(token)
