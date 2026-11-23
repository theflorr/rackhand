"""Tests for auto-update version parsing, release checking, and comparison logic."""

from unittest.mock import AsyncMock, patch
import pytest

from updater import (
    parse_version,
    is_update_available,
    check_for_update,
    ReleaseInfo,
)


def test_parse_version():
    # Titles starting with versions matching GitHub releases
    assert parse_version("v1.0 Windows compile") == (1, 0, 0)
    assert parse_version("v1.1 Windows compile") == (1, 1, 0)
    assert parse_version("v1.2.3 Windows compile") == (1, 2, 3)
    assert parse_version("v1.1") == (1, 1, 0)
    assert parse_version("1.0") == (1, 0, 0)
    assert parse_version("2.4.15") == (2, 4, 15)
    assert parse_version("") == (0, 0, 0)


def test_is_update_available():
    # Current version is v1.1
    # 1.0 on GitHub -> False (does NOT trigger now)
    assert not is_update_available("v1.1", "v1.0 Windows compile")
    assert not is_update_available("v1.1", "v1.1 Windows compile")
    assert not is_update_available("v1.1", "1.1")
    assert not is_update_available("v1.1", "v0.9.9 Windows compile")

    # Future versions -> True (WILL trigger in the future)
    assert is_update_available("v1.1", "v1.2 Windows compile")
    assert is_update_available("v1.1", "v1.1.1 Patch release")
    assert is_update_available("v1.1", "v2.0 Next Gen")


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
        # Should be None because current v1.1 >= remote v1.0
        assert result is None


@pytest.mark.asyncio
async def test_check_for_update_triggers_when_under_remote():
    mock_rel = ReleaseInfo(
        version="v1.2",
        title="v1.2 Windows compile",
        tag="windows-build",
        html_url="https://github.com/theflorr/rackhand/releases/tag/windows-build",
        asset_name="rackhand.exe",
        asset_download_url="https://github.com/theflorr/rackhand/releases/download/windows-build/rackhand.exe",
        asset_size=26000000,
        published_at="2026-11-25",
        body="Feature update",
    )
    with patch("updater.fetch_latest_release", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_rel
        result = await check_for_update("v1.1")
        # Should return ReleaseInfo because current v1.1 < remote v1.2
        assert result is not None
        assert result.version == "v1.2"
        assert result.title == "v1.2 Windows compile"
