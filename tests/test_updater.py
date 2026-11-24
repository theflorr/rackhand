"""Tests for auto-update version parsing, release checking, and comparison logic across v1.1 and v1.1.3 formats."""

from unittest.mock import AsyncMock, patch
import pytest

from updater import (
    format_version,
    parse_version,
    is_update_available,
    check_for_update,
    ReleaseInfo,
)


def test_parse_version():
    # Two-part format (v1.1, 1.1)
    assert parse_version("v1.0 Windows compile") == (1, 0, 0)
    assert parse_version("v1.1 Windows compile") == (1, 1, 0)
    assert parse_version("v1.1") == (1, 1, 0)
    assert parse_version("1.0") == (1, 0, 0)
    assert parse_version("1.1") == (1, 1, 0)

    # Three-part format (v1.1.3, 1.1.3)
    assert parse_version("v1.1.3") == (1, 1, 3)
    assert parse_version("1.1.3") == (1, 1, 3)
    assert parse_version("v1.2.3 Windows compile") == (1, 2, 3)
    assert parse_version("Release v1.1.3") == (1, 1, 3)
    assert parse_version("rackhand-v1.1.3") == (1, 1, 3)
    assert parse_version("tag_v1.1.3") == (1, 1, 3)
    assert parse_version("v1.1.3-beta") == (1, 1, 3)
    assert parse_version("(v1.1.3)") == (1, 1, 3)
    assert parse_version("2.4.15") == (2, 4, 15)
    assert parse_version("") == (0, 0, 0)


def test_format_version():
    # Two-part format output when patch is 0
    assert format_version((1, 1, 0)) == "v1.1"
    assert format_version((1, 0, 0)) == "v1.0"
    assert format_version("1.1") == "v1.1"
    assert format_version("v1.1") == "v1.1"

    # Three-part format output when patch is non-zero
    assert format_version((1, 1, 3)) == "v1.1.3"
    assert format_version("v1.1.3") == "v1.1.3"
    assert format_version("1.1.3") == "v1.1.3"
    assert format_version("1.2.5") == "v1.2.5"

    # Optional explicit patch output even if 0
    assert format_version((1, 1, 0), include_patch_if_zero=True) == "v1.1.0"


def test_is_update_available():
    # Comparing v1.1 vs v1.1.3
    # When current is v1.1 (1.1.0):
    assert is_update_available("v1.1", "v1.1.3")  # 1.1.3 is newer than 1.1.0 -> True
    assert is_update_available("v1.1", "1.1.3")
    assert is_update_available("v1.1", "v1.1.1")
    assert is_update_available("v1.1", "v1.2")
    assert not is_update_available("v1.1", "v1.1")
    assert not is_update_available("v1.1", "1.1")
    assert not is_update_available("v1.1", "v1.0")
    assert not is_update_available("v1.1", "v0.9.9")

    # When current is v1.1.3 (1.1.3):
    assert not is_update_available("v1.1.3", "v1.1")  # 1.1.0 is older than 1.1.3 -> False
    assert not is_update_available("v1.1.3", "v1.1.3")
    assert not is_update_available("v1.1.3", "v1.1.2")
    assert is_update_available("v1.1.3", "v1.1.4")  # newer patch -> True
    assert is_update_available("v1.1.3", "v1.2")    # newer minor -> True
    assert is_update_available("v1.1.3", "v2.0.0")  # newer major -> True


@pytest.mark.asyncio
async def test_check_for_update_no_update_when_at_or_ahead():
    mock_rel = ReleaseInfo(
        version="v1.0",
        title="v1.0 Windows compile",
        tag="windows-build",
        html_url="https://github.com/theflorr/rackhand/releases/tag/windows-build",
        asset_name="rackhand.exe",
        asset_download_url="https://github.com/theflorr/rackhand/releases/download/windows-build/rackhand.exe",
        asset_size=25870336,
        published_at="2026-11-23",
        body="Initial release",
    )
    with patch("updater.fetch_latest_release", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_rel
        result = await check_for_update("v1.1")
        assert result is None

        # Also when running on v1.1.3
        result_3 = await check_for_update("v1.1.3")
        assert result_3 is None


@pytest.mark.asyncio
async def test_check_for_update_triggers_for_v1_1_to_v1_1_3():
    # Current version is v1.1, remote releases v1.1.3
    mock_rel = ReleaseInfo(
        version="v1.1.3",
        title="v1.1.3 Patch release",
        tag="v1.1.3",
        html_url="https://github.com/theflorr/rackhand/releases/tag/v1.1.3",
        asset_name="rackhand.exe",
        asset_download_url="https://github.com/theflorr/rackhand/releases/download/v1.1.3/rackhand.exe",
        asset_size=26000000,
        published_at="2026-11-25",
        body="Fix session persistence",
    )
    with patch("updater.fetch_latest_release", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_rel
        # Running on v1.1 should detect v1.1.3 as an update
        result = await check_for_update("v1.1")
        assert result is not None
        assert result.version == "v1.1.3"

        # Running on v1.1.3 itself should NOT trigger
        result_same = await check_for_update("v1.1.3")
        assert result_same is None


@pytest.mark.asyncio
async def test_check_for_update_triggers_when_tag_has_version_but_title_is_generic():
    # If GitHub release name is generic like "Windows compile", but tag is "v1.1.3"
    mock_rel = ReleaseInfo(
        version="v1.1.3",
        title="Windows compile",
        tag="v1.1.3",
        html_url="https://github.com/theflorr/rackhand/releases/tag/v1.1.3",
        asset_name="rackhand.exe",
        asset_download_url="https://github.com/theflorr/rackhand/releases/download/v1.1.3/rackhand.exe",
        asset_size=26000000,
        published_at="2026-11-25",
        body="Automated build",
    )
    with patch("updater.fetch_latest_release", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_rel
        result = await check_for_update("v1.1")
        assert result is not None
        assert result.version == "v1.1.3"


def test_find_current_executable_with_pyapp_env(tmp_path, monkeypatch):
    from updater import find_current_executable
    mock_exe = tmp_path / "rackhand.exe"
    mock_exe.touch()
    monkeypatch.setenv("PYAPP", str(mock_exe))
    found = find_current_executable()
    assert found == mock_exe.resolve()


def test_find_current_executable_with_sys_frozen(tmp_path, monkeypatch):
    import sys
    from updater import find_current_executable
    mock_exe = tmp_path / "installed_app.exe"
    mock_exe.touch()
    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(mock_exe))
    found = find_current_executable()
    assert found == mock_exe.resolve()


def test_find_current_executable_fallback_when_python_interpreter(monkeypatch):
    import sys
    from updater import find_current_executable
    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr("shutil.which", lambda _: None)
    # When no rackhand.exe in cwd
    with monkeypatch.context() as m:
        m.chdir("/tmp")
        found = find_current_executable()
        assert found is None or found.name.lower() == "rackhand.exe"
