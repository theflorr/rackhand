import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from models import ApiScheme, AuthStyle, ProviderConfig
from llm_engine import build_llm_request
from ui.modals.providers import _clean_privacy_text
import subscription_detector


def test_build_llm_request_claude_oauth():
    # OAuth token starting with sk-ant-oat
    provider = ProviderConfig(
        key="anthropic",
        name="Claude",
        scheme=ApiScheme.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="sk-ant-oat01-test-token-12345",
        auth=AuthStyle.BEARER,
        model="claude-3-5-sonnet-20241022",
    )
    endpoint, headers, payload, is_anthropic = build_llm_request(
        provider, [{"role": "user", "content": "hi"}], max_tokens=10
    )
    assert is_anthropic is True
    assert endpoint == "https://api.anthropic.com/v1/messages"
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer sk-ant-oat01-test-token-12345"
    assert headers.get("anthropic-beta") == "oauth-2025-04-20"
    assert headers.get("user-agent") == "claude-code/0.2.29"
    assert "x-api-key" not in headers


def test_build_llm_request_claude_console_api_key():
    # Standard console API key
    provider = ProviderConfig(
        key="anthropic",
        name="Claude",
        scheme=ApiScheme.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="sk-ant-api03-standard-key-12345",
        auth=AuthStyle.X_API_KEY,
        model="claude-3-5-sonnet-20241022",
    )
    endpoint, headers, payload, is_anthropic = build_llm_request(
        provider, [{"role": "user", "content": "hi"}], max_tokens=10
    )
    assert is_anthropic is True
    assert endpoint == "https://api.anthropic.com/v1/messages"
    assert headers.get("x-api-key") == "sk-ant-api03-standard-key-12345"
    assert "Authorization" not in headers
    assert "anthropic-beta" not in headers


def test_privacy_cleaner_removes_email():
    raw_desc = "Claude Code (.credentials.json) - developer.john+1@example.com (stripe_subscription)"
    cleaned = _clean_privacy_text(raw_desc)
    assert "developer.john" not in cleaned
    assert "example.com" not in cleaned
    assert "@" not in cleaned
    assert "Claude Code (.credentials.json)" in cleaned

    another = "Local subscription: user@gmail.com"
    cleaned2 = _clean_privacy_text(another)
    assert "@" not in cleaned2
    assert "user" not in cleaned2


def test_detect_claude_cli_no_email_and_oauth_bearer(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    creds_file = claude_dir / ".credentials.json"
    creds_content = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-my-claude-code-token",
            "subscriptionType": "stripe_subscription",
        },
        "oauthAccount": {
            "emailAddress": "secretuser@gmail.com",
            "billingType": "stripe_subscription",
        }
    }
    creds_file.write_text(json.dumps(creds_content))

    with patch.object(subscription_detector, "_get_candidate_dirs", return_value=[tmp_path]):
        det = subscription_detector.detect_claude()
        assert det is not None
        assert det.auth == AuthStyle.BEARER
        assert det.api_key == "sk-ant-oat01-my-claude-code-token"
        assert "@" not in det.account_info
        assert "secretuser" not in det.account_info
        assert "Stripe Subscription" in det.account_info or "Subscription" in det.account_info


def test_detect_antigravity_no_email():
    with patch.object(subscription_detector, "_find_agy_executable", return_value="/usr/local/bin/agy"), \
         patch.object(subscription_detector, "_read_win_credential", return_value=("user@gmail.com", json.dumps({"id_token_email": "user@gmail.com"}).encode("utf-8"))):
        det = subscription_detector.detect_antigravity()
        assert det is not None
        assert "@" not in det.account_info
        assert "user@gmail.com" not in det.account_info
        assert "email" not in det.raw_info
