"""Test Plan Mode functionality: agents, protocol, mutating command blocking, and slash commands."""

import pytest
from unittest.mock import MagicMock
from textual._context import active_app
from models import DeviceSession, DeviceProfile, Vendor, Transport, ProposedChange, Risk, ProviderConfig, Settings
from agents import (
    AGENTS, PLAN_AGENTS, ALL_AGENTS,
    to_plan_agent, to_build_agent, is_plan_agent,
    resolve_agent, get_agent, get_agent_options,
)
from llm_engine import is_mutating_cli_command, PLAN_NETWORK_TOOLS, NETWORK_TOOLS, RealLLMEngine


def test_agent_mappings_and_counts():
    assert len(AGENTS) == 11
    assert len(PLAN_AGENTS) == 11
    assert len(ALL_AGENTS) == 22

    for a in AGENTS:
        plan_id = to_plan_agent(a.id)
        assert is_plan_agent(plan_id)
        assert plan_id.endswith("-plan")
        build_id = to_build_agent(plan_id)
        assert build_id == a.id
        assert not is_plan_agent(build_id)

        # Ensure both are registered in get_agent
        assert get_agent(a.id) is not None
        assert get_agent(plan_id) is not None

        # Verify plan agent prompt includes PLAN_MODE_PROTOCOL
        plan_spec = get_agent(plan_id)
        assert "PLAN MODE PROTOCOL" in plan_spec.system_prompt


def test_resolve_agent_with_session():
    # Test Auto in build mode
    cisco_session = DeviceSession(
        id="dev1",
        profile=DeviceProfile(name="cisco-switch", vendor=Vendor.CISCO_IOS, transport=Transport.SERIAL),
        hostname="Switch-Core-1",
    )
    spec = resolve_agent("auto", cisco_session)
    assert spec.id == "cisco-ios"

    # Test Auto in plan mode
    plan_spec = resolve_agent("auto-plan", cisco_session)
    assert plan_spec.id == "cisco-ios-plan"
    assert is_plan_agent(plan_spec.id)

    # Test Fortinet in plan mode
    forti_session = DeviceSession(
        id="dev2",
        profile=DeviceProfile(name="fgt-edge", vendor=Vendor.FORTIOS, transport=Transport.SERIAL),
        hostname="fgt-edge-fw01",
    )
    forti_plan_spec = resolve_agent("auto-plan", forti_session)
    assert forti_plan_spec.id == "fortigate-plan"


def test_get_agent_options():
    build_opts = get_agent_options(is_plan_mode=False)
    assert len(build_opts) == 11
    assert build_opts[0][1] == "auto"

    plan_opts = get_agent_options(is_plan_mode=True)
    assert len(plan_opts) == 11
    assert plan_opts[0][1] == "auto-plan"
    assert "Plan" in plan_opts[0][0]


def test_mutating_command_blocker():
    # Allowed read-only commands
    read_cmds = [
        "show ip interface brief",
        "show running-config",
        "show version",
        "get system status",
        "diagnose sys top",
        "show vlan",
        "ping 8.8.8.8",
        "traceroute 1.1.1.1",
        "show mac address-table",
    ]
    for cmd in read_cmds:
        assert not is_mutating_cli_command(cmd), f"Should allow read-only: {cmd}"

    # Mutating commands that MUST be blocked
    mutating_cmds = [
        "configure terminal",
        "conf t",
        "write memory",
        "wr",
        "wr mem",
        "copy running-config startup-config",
        "reload",
        "reboot",
        "delete vlan.dat",
        "set system dns",
        "config router ospf",
        "erase startup-config",
        "format flash:",
        "install activate",
        "rollback running-config",
    ]
    for cmd in mutating_cmds:
        assert is_mutating_cli_command(cmd), f"Should block mutating: {cmd}"


def test_llm_engine_build_request_tools():
    engine = RealLLMEngine()
    provider = ProviderConfig(
        key="test-prov",
        name="Test",
        model="test-model",
        api_key="sk-test",
        supports_tools=True,
    )

    # In normal build mode, tools should contain propose_change and run_command
    _, _, payload_build = engine._build_request(provider, [{"role": "user", "content": "hi"}], tools=NETWORK_TOOLS)
    tool_names_build = [t["function"]["name"] for t in payload_build["tools"]]
    assert "run_command" in tool_names_build
    assert "propose_change" in tool_names_build

    # In plan mode, tools should only contain run_command
    _, _, payload_plan = engine._build_request(provider, [{"role": "user", "content": "hi"}], tools=PLAN_NETWORK_TOOLS)
    tool_names_plan = [t["function"]["name"] for t in payload_plan["tools"]]
    assert "run_command" in tool_names_plan
    assert "propose_change" not in tool_names_plan


def test_ui_workspace_plan_mode_toggle():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App

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
        screen._run_agent = MagicMock()

        assert screen.current_mode == "build"
        assert screen.active_agent_id == "auto"

        # Execute /plan -> enters plan mode
        screen._handle_command("/plan")
        assert screen.current_mode == "plan"
        assert screen.active_agent_id == "auto-plan"
        screen._bubble.assert_not_called()  # Banner mounted via _mount_message

        # Execute /plan again -> verify it warns and directs to /build ("only tell dont do")
        screen._handle_command("/plan")
        assert screen.current_mode == "plan"
        assert screen.active_agent_id == "auto-plan"
        assert screen._bubble.call_count == 1  # Warning panel shown!

        # Execute /build -> returns to build mode
        screen._handle_command("/build")
        assert screen.current_mode == "build"
        assert screen.active_agent_id == "auto"

        # Execute /build again -> shows already in build mode
        screen._handle_command("/build")
        assert screen.current_mode == "build"
        assert screen._bubble.call_count == 2
    finally:
        active_app.reset(token)
