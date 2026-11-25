"""Rackhand Auto-Update Engine

Checks GitHub Releases for updates based on release titles starting with a version,
compares against the running version, and handles download and binary replacement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

logger = logging.getLogger("rackhand.updater")

GITHUB_REPO = "theflorr/rackhand"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"


def parse_version(text: str) -> tuple[int, ...]:
    """Extract semantic version integers from a string.
    
    Supports both two-part ('v1.1', '1.1') and three-part ('v1.1.3', '1.1.3') formats,
    as well as versions embedded in strings like 'Release v1.1.3' or 'v1.1 Windows compile'.
    
    Examples:
        'v1.0 Windows compile' -> (1, 0, 0)
        'v1.1'                 -> (1, 1, 0)
        'v1.1.3'               -> (1, 1, 3)
        '1.1'                  -> (1, 1, 0)
        '1.1.3'                -> (1, 1, 3)
    """
    if not text:
        return (0, 0, 0)

    # Match version pattern like v1.1, v1.1.3, 1.1, 1.1.3
    m = re.search(r"(?:^|[^0-9a-zA-Z])v?(\d+(?:\.\d+)+)(?:[^0-9a-zA-Z]|$)", text.strip())
    if m:
        parts = [int(p) for p in m.group(1).split(".")]
    else:
        m_single = re.search(r"(?:^|[^0-9a-zA-Z])v?(\d+)(?:[^0-9a-zA-Z]|$)", text.strip())
        parts = [int(m_single.group(1))] if m_single else [0]

    # Normalize to at least 3 parts (major, minor, patch)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def format_version(version: str | tuple[int, ...] | list[int], include_patch_if_zero: bool = False) -> str:
    """Format a version into canonical representation.
    
    Supports both 2-part format ('v1.1') and 3-part format ('v1.1.3'):
        format_version((1, 1, 0)) -> 'v1.1'
        format_version((1, 1, 3)) -> 'v1.1.3'
        format_version('1.1')     -> 'v1.1'
        format_version('v1.1.3')  -> 'v1.1.3'
        format_version('1.1.0', include_patch_if_zero=True) -> 'v1.1.0'
    """
    if isinstance(version, str):
        parsed = parse_version(version)
    elif isinstance(version, (tuple, list)):
        parsed = tuple(version)
    else:
        parsed = (0, 0, 0)

    while len(parsed) < 3:
        parsed = parsed + (0,)

    major, minor, patch = parsed[0], parsed[1], parsed[2]
    if patch != 0 or include_patch_if_zero:
        return f"v{major}.{minor}.{patch}"
    return f"v{major}.{minor}"


def is_update_available(current_version: str, remote_title: str) -> bool:
    """Return True if remote_title represents a version higher than current_version.
    
    Seamlessly compares both two-part ('v1.1') and three-part ('v1.1.3') formats:
        is_update_available('v1.1', 'v1.1.3') -> True
        is_update_available('v1.1.3', 'v1.2') -> True
        is_update_available('v1.1.3', 'v1.1.4') -> True
        is_update_available('v1.1.3', 'v1.1') -> False
        is_update_available('v1.1', 'v1.1')   -> False
        is_update_available('v1.1.3', 'v1.1.3') -> False
    """
    curr = parse_version(current_version)
    remote = parse_version(remote_title)
    return remote > curr


@dataclass
class ReleaseInfo:
    """Parsed release information from GitHub Releases."""
    version: str
    title: str
    tag: str
    html_url: str
    asset_name: Optional[str]
    asset_download_url: Optional[str]
    asset_size: int
    published_at: str
    body: str


async def fetch_latest_release(timeout: float = 10.0) -> Optional[ReleaseInfo]:
    """Query GitHub API for releases and return the latest published release."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                RELEASES_API_URL,
                headers={"User-Agent": "Rackhand-Updater", "Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code != 200:
                return None
            releases = resp.json()
            if not isinstance(releases, list) or not releases:
                return None

            # Get all non-draft releases
            valid_releases = [r for r in releases if not r.get("draft")]
            if not valid_releases:
                valid_releases = releases

            # Select release with highest semantic version, or the first published release
            best_release = None
            best_ver = (0, 0, 0)
            for r in valid_releases:
                v_cand = max(parse_version(r.get("name") or ""), parse_version(r.get("tag_name") or ""))
                if v_cand > best_ver:
                    best_ver = v_cand
                    best_release = r

            latest = best_release or valid_releases[0]
            name = latest.get("name") or ""
            tag = latest.get("tag_name") or ""
            title = name or tag or ""

            # Parse version from either name or tag (whichever has valid semantic version)
            parsed = parse_version(name) if parse_version(name) > (0, 0, 0) else parse_version(tag)
            ver_str = format_version(parsed) if parsed > (0, 0, 0) else (name or tag or "v0.0")

            # Locate Windows rackhand.exe asset if present
            assets = latest.get("assets", [])
            exe_asset = next((a for a in assets if a.get("name", "").lower() == "rackhand.exe"), None)
            if not exe_asset and assets:
                exe_asset = next((a for a in assets if a.get("name", "").lower().endswith(".exe")), None)

            asset_name = exe_asset.get("name") if exe_asset else None
            asset_url = exe_asset.get("browser_download_url") if exe_asset else None
            asset_size = exe_asset.get("size", 0) if exe_asset else 0

            return ReleaseInfo(
                version=ver_str,
                title=title,
                tag=latest.get("tag_name", ""),
                html_url=latest.get("html_url", f"https://github.com/{GITHUB_REPO}/releases"),
                asset_name=asset_name,
                asset_download_url=asset_url,
                asset_size=asset_size,
                published_at=latest.get("published_at", "")[:10],
                body=latest.get("body", "") or "",
            )
    except Exception:
        return None


async def check_for_update(current_version: str) -> Optional[ReleaseInfo]:
    """Check if a release with a higher version is published on GitHub.
    
    Supports current versions in 'v1.1' or 'v1.1.3' format and compares
    against remote release versions, tags, and titles.
    """
    rel = await fetch_latest_release()
    if not rel:
        return None
    for cand in (rel.version, rel.tag, rel.title):
        if cand and is_update_available(current_version, cand):
            return rel
    return None


async def download_update(
    download_url: str,
    target_path: Path,
    progress_cb: Optional[Callable[[float, int, int], None]] = None,
) -> bool:
    """Download the new executable from GitHub with progress callback."""
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass

        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            async with client.stream("GET", download_url, headers={"User-Agent": "Rackhand-Updater"}) as resp:
                if resp.status_code != 200:
                    logger.warning("Download HTTP status %d for %s", resp.status_code, download_url)
                    return False
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(target_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total > 0:
                            try:
                                progress_cb(downloaded / total, downloaded, total)
                            except Exception:
                                pass
        if total > 0 and downloaded < total:
            logger.warning("Truncated download for %s (%d/%d bytes)", download_url, downloaded, total)
            target_path.unlink(missing_ok=True)
            return False
        if target_path.suffix.lower() == ".exe":
            with open(target_path, "rb") as f:
                if f.read(2) != b"MZ":
                    logger.warning("Downloaded .exe failed PE magic header check: %s", target_path)
                    target_path.unlink(missing_ok=True)
                    return False
        return True
    except Exception as exc:
        logger.warning("Download failed for %s -> %s: %s", download_url, target_path, exc, exc_info=True)
        if target_path.exists():
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass
        return False


def _get_process_exe_windows(pid: int) -> Optional[Path]:
    """Retrieve full executable image path for a given process ID on Windows using kernel32."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                p = Path(buf.value).resolve()
                if p.is_file():
                    return p
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return None


def _is_python_runtime(name: str) -> bool:
    n = name.lower()
    return n.startswith("python") or n.startswith("pypy")


def is_running_from_source() -> bool:
    """Return True if Rackhand is running directly from source rather than a compiled executable."""
    # 1. Standalone frozen binary (PyInstaller, cx_Freeze)
    if getattr(sys, "frozen", False):
        return False

    # 2. PyApp passes exact launcher path in PYAPP env var
    pyapp_env = os.environ.get("PYAPP", "").strip()
    if pyapp_env and pyapp_env != "1":
        return False

    # 3. Check sys.executable if it is rackhand.exe (or anything other than python runtime)
    exe_name = Path(sys.executable).name.lower()
    if not _is_python_runtime(exe_name):
        return False

    # 4. Check parent process on Windows (PyApp launcher process is parent of python runtime)
    if sys.platform == "win32":
        try:
            ppid = os.getppid()
            if ppid > 0:
                parent_exe = _get_process_exe_windows(ppid)
                if parent_exe and parent_exe.is_file() and not _is_python_runtime(parent_exe.name):
                    return False
        except Exception:
            pass

    return True


def find_current_executable() -> Optional[Path]:
    """Find the path to the running rackhand executable across PyApp, PyInstaller, or custom locations."""
    # 1. PyApp passes exact launcher path in PYAPP env var when PYAPP_PASS_LOCATION=1
    pyapp_env = os.environ.get("PYAPP", "").strip()
    if pyapp_env and pyapp_env != "1":
        cand = Path(pyapp_env).resolve()
        if cand.is_file() and not _is_python_runtime(cand.name):
            return cand

    # 2. Check sys.frozen (PyInstaller standalone binary)
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve()
        if cand.is_file():
            return cand

    # 3. Check sys.executable if it is rackhand.exe (or anything other than python runtime)
    cand = Path(sys.executable).resolve()
    if cand.is_file() and not _is_python_runtime(cand.name):
        return cand

    # 4. Check parent process on Windows (PyApp launcher process is parent of python runtime)
    if sys.platform == "win32":
        try:
            ppid = os.getppid()
            if ppid > 0:
                parent_exe = _get_process_exe_windows(ppid)
                if parent_exe and parent_exe.is_file() and not _is_python_runtime(parent_exe.name):
                    return parent_exe
        except Exception:
            pass

    # 5. Look for rackhand.exe in current working directory or system PATH
    cwd_exe = Path.cwd() / "rackhand.exe"
    if cwd_exe.is_file():
        return cwd_exe

    which_exe = shutil.which("rackhand.exe")
    if which_exe:
        p = Path(which_exe).resolve()
        if p.is_file():
            return p

    return None


def apply_windows_update_and_restart(new_exe_path: Path) -> None:
    """Spawn an out-of-process helper script to overwrite current exe and restart Rackhand."""
    current_exe = find_current_executable()
    
    # If running inside plain python development environment, don't overwrite python.exe
    if not current_exe or not current_exe.is_file() or current_exe.name.lower() in ("python.exe", "pythonw.exe"):
        target_standalone = Path.cwd() / "rackhand.exe"
        if new_exe_path.exists():
            shutil.move(str(new_exe_path), str(target_standalone))
        return

    # Windows running executables cannot be deleted directly while running,
    # but can be replaced via a batch loop after the process exits.
    batch_script = new_exe_path.parent / "_rackhand_apply_update.cmd"
    # Avoid enabledelayedexpansion so paths containing '!' are never mangled on Windows.
    src_str = str(new_exe_path).replace('"', "")
    dst_str = str(current_exe).replace('"', "")
    batch_content = f"""@echo off
timeout /t 1 /nobreak >nul
set RETRY=0
:retry
set /a RETRY+=1
move /y "{src_str}" "{dst_str}" >nul 2>&1
if errorlevel 1 (
    if %RETRY% leq 30 (
        timeout /t 1 /nobreak >nul
        goto retry
    )
    exit /b 1
)
start "" "{dst_str}"
del "%~f0"
"""
    batch_script.write_text(batch_content, encoding="utf-8", errors="replace")
    subprocess.Popen(
        ["cmd.exe", "/c", str(batch_script)],
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    sys.exit(0)
