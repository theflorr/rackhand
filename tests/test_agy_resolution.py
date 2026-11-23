import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from models import find_agy_executable, ensure_agy_in_path, ProviderConfig, ApiScheme, AuthStyle, DeviceSession, DeviceProfile, Vendor, Transport, Settings
from llm_engine import RealLLMEngine
from models import AgentError


def test_find_agy_executable_with_direct_file(tmp_path):
    dummy_exe = tmp_path / "dummy_agy.exe"
    dummy_exe.write_text("dummy")

    resolved = find_agy_executable(str(dummy_exe))
    assert resolved == str(dummy_exe)


def test_find_agy_executable_with_hint_none_or_missing(tmp_path):
    with patch("shutil.which", return_value=None):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path), "PATH": "", "USERPROFILE": str(tmp_path)}, clear=False):
            # Create candidate in tmp_path / "agy" / "bin" / "agy.exe"
            candidate = tmp_path / "agy" / "bin" / "agy.exe"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("binary")

            found = find_agy_executable()
            assert found == str(candidate)


def test_ensure_agy_in_path(tmp_path):
    fake_bin_dir = tmp_path / "custom_bin"
    fake_bin_dir.mkdir()
    fake_exe = fake_bin_dir / "agy.exe"
    fake_exe.write_text("bin")

    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = "C:\\Windows\\System32"
        resolved = ensure_agy_in_path(str(fake_exe))
        assert resolved == str(fake_exe)
        assert str(fake_bin_dir).lower() in os.environ["PATH"].lower()
    finally:
        os.environ["PATH"] = old_path


@pytest.mark.asyncio
async def test_llm_engine_agy_not_found():
    engine = RealLLMEngine()
    provider = ProviderConfig(
        key="antigravity",
        name="Google Antigravity",
        spec_id="antigravity",
        scheme=ApiScheme.ANTIGRAVITY,
        base_url="local:agy",
        api_key="non_existent_path_to_agy_xyz",
    )
    profile = DeviceProfile(name="test", transport=Transport.SERIAL)
    session = DeviceSession(id="s1", profile=profile, vendor="ios")

    async def fake_run_command(cmd: str):
        return None

    async def fake_propose_change(prop):
        return None

    with patch("models.find_agy_executable", return_value=None):
        events = []
        async for ev in engine._run_antigravity_turn(
            prompt="Hello",
            session=session,
            provider=provider,
            settings=Settings(),
            agent_spec=None,
            run_command_fn=fake_run_command,
            propose_change_fn=fake_propose_change,
            session_id="s1",
            is_plan_mode=False,
        ):
            events.append(ev)

        assert any(isinstance(e, AgentError) and "not found" in e.message.lower() for e in events)
