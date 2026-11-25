import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from models import ApiScheme, AuthStyle, DeviceProfile, DeviceSession, ProposedChange, ProviderConfig, Risk, Transport, Vendor
from usage_tracker import ApiQuotaInfo, RateLimitInfo, TokenUsageEntry
from vault import Vault
from llm_engine import NETWORK_TOOLS, _parse_llm_response, build_llm_request
from reviewer import review_danger_only
from snapshots import _sanitize_dirname


def test_usage_tracker_vault_roundtrip(tmp_path):
    v = Vault(tmp_path / "vault.rhv")
    v.create_vault("Strong-Passphrase-2026!", opslimit=1, memlimit=8192)
    entry = TokenUsageEntry(timestamp=1234.5, provider_key="anthropic", model="claude-3-5-sonnet", tokens_in=10, tokens_out=20, session_id="s1")
    rl = RateLimitInfo(limit_requests=100, remaining_requests=95, reset_requests_s=12.0, last_updated=1000.0)
    aq = ApiQuotaInfo(credits_used=1.5, credits_remaining=8.5, plan_name="Pro", last_checked=1000.0)

    v.save_usage([entry.to_dict()], {"anthropic": rl.to_dict()}, {"anthropic": aq.to_dict()})
    entries_raw, rl_raw, aq_raw = v.load_usage()
    v.close()

    assert len(entries_raw) == 1
    loaded_entry = TokenUsageEntry.from_dict(entries_raw[0])
    assert loaded_entry.provider_key == "anthropic"
    assert loaded_entry.tokens_in == 10
    assert loaded_entry.tokens_out == 20
    assert RateLimitInfo.from_dict(rl_raw["anthropic"]).remaining_requests == 95
    assert ApiQuotaInfo.from_dict(aq_raw["anthropic"]).credits_remaining == 8.5


def test_anthropic_request_and_response_parsing():
    prov = ProviderConfig(
        key="anthropic",
        name="Anthropic",
        scheme=ApiScheme.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="sk-ant-api03-test",
        auth=AuthStyle.X_API_KEY,
        model="claude-3-5-sonnet-20241022",
    )
    msgs = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Check interfaces"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run_command", "arguments": '{"command": "show ip int br"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Gi0/0 up up"},
    ]
    endpoint, headers, payload, is_anthropic = build_llm_request(prov, msgs, tools=NETWORK_TOOLS)
    assert is_anthropic is True
    assert endpoint.endswith("/v1/messages")
    assert headers["x-api-key"] == "sk-ant-api03-test"
    assert payload["system"] == "Be concise."
    assert all("input_schema" in t for t in payload["tools"])
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"

    content, tool_calls, in_toks, out_toks = _parse_llm_response(
        {
            "content": [
                {"type": "text", "text": "Checking now."},
                {"type": "tool_use", "id": "toolu_1", "name": "run_command", "input": {"command": "show version"}},
            ],
            "usage": {"input_tokens": 42, "output_tokens": 13},
        },
        is_anthropic=True,
    )
    assert content == "Checking now."
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "run_command"
    assert in_toks == 42 and out_toks == 13


@pytest.mark.asyncio
async def test_review_danger_only_fails_closed():
    mock_llm = MagicMock()
    mock_llm.call_llm_stateless = AsyncMock(return_value=None)
    prov = ProviderConfig(key="test", name="Test", model="test-model")
    sess = DeviceSession(id="d1", profile=DeviceProfile(name="R1", vendor=Vendor.CISCO_IOS, transport=Transport.SSH), hostname="R1", vendor=Vendor.CISCO_IOS)
    change = ProposedChange(id="c1", device="R1", title="Remove BGP", commands=["no router bgp 65000"], rationale="remove bgp", risk=Risk.CONFIG)

    verdict = await review_danger_only(change, sess, prov, mock_llm)
    assert verdict.auto_approve is False
    assert verdict.is_dangerous is True


def test_snapshot_sanitize_dirname_windows_safe():
    assert _sanitize_dirname("..\\..\\Windows\\System32") == "____Windows_System32"
    assert _sanitize_dirname("Core:Switch*01?") == "Core_Switch_01_"
    assert _sanitize_dirname("...") == "_"
    assert _sanitize_dirname("") == "device"


@pytest.mark.asyncio
async def test_action_compact_queues_compact_when_busy():
    from textual.app import App
    from backend import RealBackend
    from models import Settings
    from ui.screens.workspace import WorkspaceScreen
    from ui.styles import APP_CSS

    class _MockApp(App):
        CSS = APP_CSS

        def __init__(self) -> None:
            super().__init__()
            self.sessions = []
            self.active = None
            self.active_id = None
            self.provider = ProviderConfig(key="test", name="Test", model="test-model")
            self.settings = Settings()
            self.backend = RealBackend()

        def on_mount(self) -> None:
            self.push_screen(WorkspaceScreen())

    app = _MockApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        scr = app.screen
        scr._busy = True
        scr.action_compact()
        assert len(scr._prompt_queue) == 1
        assert scr._prompt_queue[0].display_text == "/compact"
        assert scr._prompt_queue[0].expanded_text == "/compact"
