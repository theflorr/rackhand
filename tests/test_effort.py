"""Unit tests for /effort, /variant, and /thinking model effort selection."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from models import ProviderConfig, Settings
from llm_engine import RealLLMEngine
from ui.suggestions import get_suggestions


def test_settings_reasoning_effort_serialization():
    s = Settings()
    assert s.reasoning_effort == "medium"

    d = s.to_dict()
    assert d["reasoning_effort"] == "medium"

    d["reasoning_effort"] = "high"
    restored = Settings.from_dict(d)
    assert restored.reasoning_effort == "high"


def test_llm_engine_build_request_effort():
    engine = RealLLMEngine()
    prov = ProviderConfig(key="openrouter", name="OpenRouter", model="anthropic/claude-3.7-sonnet", base_url="https://openrouter.ai/api/v1")
    messages = [{"role": "user", "content": "hello"}]

    # 1. Medium effort (default)
    s_med = Settings(reasoning_effort="medium")
    _, _, payload_med = engine._build_request(prov, messages, settings=s_med)
    assert payload_med.get("reasoning_effort") == "medium"
    assert payload_med.get("reasoning") == {"effort": "medium"}

    # 2. High effort
    s_high = Settings(reasoning_effort="high")
    _, _, payload_high = engine._build_request(prov, messages, settings=s_high)
    assert payload_high.get("reasoning_effort") == "high"
    assert payload_high.get("reasoning") == {"effort": "high"}

    # 3. Max effort
    s_max = Settings(reasoning_effort="max")
    _, _, payload_max = engine._build_request(prov, messages, settings=s_max)
    assert payload_max.get("reasoning_effort") == "high"
    assert payload_max.get("reasoning") == {"effort": "max"}

    # 4. Off / minimal effort
    s_off = Settings(reasoning_effort="off")
    _, _, payload_off = engine._build_request(prov, messages, settings=s_off)
    assert "reasoning_effort" not in payload_off
    assert "reasoning" not in payload_off


def test_suggestions_effort_and_aliases():
    # Top-level suggestions
    suggestions = get_suggestions("/")
    values = [s.value for s in suggestions]
    assert "/effort" in values
    assert "/variant" in values
    assert "/thinking" in values

    # Subargument completions
    eff_subs = get_suggestions("/effort ")
    sub_vals = [s.value for s in eff_subs]
    assert "/effort low" in sub_vals
    assert "/effort medium" in sub_vals
    assert "/effort high" in sub_vals
    assert "/effort max" in sub_vals
    assert "/effort off" in sub_vals

    var_subs = get_suggestions("/variant ")
    assert any(s.value == "/variant high" for s in var_subs)

    think_subs = get_suggestions("/thinking ")
    assert any(s.value == "/thinking max" for s in think_subs)


def test_ui_workspace_effort_commands_and_aliases():
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
            self.settings = Settings(reasoning_effort="medium")
            self.backend = MagicMock()

        def apply_settings(self, s: Settings):
            self.settings = s

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen._bubble = MagicMock()
        screen.notify = MagicMock()

        # 1. Direct command: /effort high
        screen._handle_command("/effort high")
        assert app.settings.reasoning_effort == "high"
        screen.notify.assert_called_with("Reasoning effort set to: HIGH", title="Model Effort")

        # 2. Alias: /variant low
        screen._handle_command("/variant low")
        assert app.settings.reasoning_effort == "low"
        screen.notify.assert_called_with("Reasoning effort set to: LOW", title="Model Effort")

        # 3. Alias: /thinking max
        screen._handle_command("/thinking max")
        assert app.settings.reasoning_effort == "max"
        screen.notify.assert_called_with("Reasoning effort set to: MAX", title="Model Effort")

        # 4. Off/None: /effort off
        screen._handle_command("/effort off")
        assert app.settings.reasoning_effort == "off"
        screen.notify.assert_called_with("Reasoning effort set to: OFF", title="Model Effort")

        # 5. Invalid effort argument
        screen._handle_command("/effort super-turbo")
        # Should NOT mutate setting
        assert app.settings.reasoning_effort == "off"
        assert screen._bubble.call_count >= 1

        # 6. No arguments: /effort displays current level
        bubble_count_before = screen._bubble.call_count
        screen._handle_command("/effort")
        assert screen._bubble.call_count == bubble_count_before + 1
    finally:
        active_app.reset(token)


def test_sync_provider_model_variant():
    from ui.screens.workspace import WorkspaceScreen
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def __init__(self):
            super().__init__()
            self.sessions = []
            self.active = None
            self.active_id = None
            self.provider = ProviderConfig(key="gemini", name="Google Gemini", model="gemini-3.8-flash-low", api_key="k1")
            self.settings = Settings(reasoning_effort="low")
            self.backend = MagicMock()

        def apply_settings(self, s: Settings):
            self.settings = s

    app = DummyApp()
    token = active_app.set(app)
    try:
        screen = WorkspaceScreen()
        screen._app = app
        screen.update_bars = MagicMock()

        # 1. Setting effort to medium on a -low model should swap model to -medium
        res_med = screen._sync_provider_model_variant("medium")
        assert res_med == "gemini-3.8-flash-medium"
        assert app.provider.model == "gemini-3.8-flash-medium"

        # 2. Setting effort to high should swap model to -high
        res_high = screen._sync_provider_model_variant("high")
        assert res_high == "gemini-3.8-flash-high"
        assert app.provider.model == "gemini-3.8-flash-high"

        # 3. Setting effort to max should also map to -high
        res_max = screen._sync_provider_model_variant("max")
        # Already high, returns None because no change
        assert res_max is None

        # 4. Setting effort to low should swap back to -low
        res_low = screen._sync_provider_model_variant("low")
        assert res_low == "gemini-3.8-flash-low"
        assert app.provider.model == "gemini-3.8-flash-low"

        # 5. Non-variant suffixed model should be untouched
        app.provider.model = "anthropic/claude-3.7-sonnet"
        res_none = screen._sync_provider_model_variant("medium")
        assert res_none is None
        assert app.provider.model == "anthropic/claude-3.7-sonnet"
    finally:
        active_app.reset(token)

