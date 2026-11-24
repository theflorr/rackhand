from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageGrab = None  # type: ignore[assignment]


# Global test overrides for headless test environments
_TEST_CLIPBOARD_IMAGE: Optional[Any] = None
_TEST_CLIPBOARD_TEXT: Optional[str] = None


def set_test_clipboard(image: Optional[Any] = None, text: Optional[str] = None) -> None:
    """Set mock clipboard values for deterministic unit testing."""
    global _TEST_CLIPBOARD_IMAGE, _TEST_CLIPBOARD_TEXT
    _TEST_CLIPBOARD_IMAGE = image
    _TEST_CLIPBOARD_TEXT = text


def clear_test_clipboard() -> None:
    """Clear mock clipboard values."""
    global _TEST_CLIPBOARD_IMAGE, _TEST_CLIPBOARD_TEXT
    _TEST_CLIPBOARD_IMAGE = None
    _TEST_CLIPBOARD_TEXT = None


@dataclass
class PastedTextBlock:
    tag: str         # e.g. "[39 lines]"
    content: str     # full multiline string
    line_count: int  # 39


def get_system_clipboard_image() -> Optional[Any]:
    """Retrieve an actual PIL Image from system clipboard if present.

    Raw image paths (strings) are ignored so only real pasted images are processed.
    """
    global _TEST_CLIPBOARD_IMAGE
    if _TEST_CLIPBOARD_IMAGE is not None:
        return _TEST_CLIPBOARD_IMAGE

    if Image is None:
        return None

    # 1. Standard PIL ImageGrab
    if ImageGrab is not None and hasattr(ImageGrab, "grabclipboard"):
        try:
            grabbed = ImageGrab.grabclipboard()
            # On Windows, grabclipboard() might return a list of file paths (CF_HDROP)
            # when copying a file from Explorer. The user requested:
            # "Raw image paths should not work, only pasted images."
            # Therefore, we only accept true PIL Image objects!
            if isinstance(grabbed, Image.Image):
                return grabbed
        except Exception:
            pass

    # 2. Linux Wayland fallback via wl-paste
    if sys.platform.startswith("linux") and shutil.which("wl-paste"):
        try:
            res = subprocess.run(["wl-paste", "-t", "image/png"], capture_output=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return Image.open(io.BytesIO(res.stdout))
        except Exception:
            pass

    # 3. Linux X11 fallback via xclip
    if sys.platform.startswith("linux") and shutil.which("xclip"):
        try:
            res = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                timeout=1,
            )
            if res.returncode == 0 and res.stdout:
                return Image.open(io.BytesIO(res.stdout))
        except Exception:
            pass

    return None


def get_system_clipboard_text(app: Optional[Any] = None) -> str:
    """Retrieve text from system clipboard across Linux, Windows, and macOS."""
    global _TEST_CLIPBOARD_TEXT
    if _TEST_CLIPBOARD_TEXT is not None:
        return _TEST_CLIPBOARD_TEXT

    # 1. Windows ctypes native clipboard
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            if user32.OpenClipboard(0):
                try:
                    h_clip = user32.GetClipboardData(CF_UNICODETEXT)
                    if h_clip:
                        kernel32.GlobalLock.restype = ctypes.c_wchar_p
                        p_text = kernel32.GlobalLock(h_clip)
                        if p_text:
                            text_val = str(p_text)
                            kernel32.GlobalUnlock(h_clip)
                            return text_val
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

    # 2. Linux Wayland wl-paste
    if sys.platform.startswith("linux") and shutil.which("wl-paste"):
        try:
            res = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass

    # 3. Linux X11 xclip
    if sys.platform.startswith("linux") and shutil.which("xclip"):
        try:
            res = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass

    # 4. Linux xsel
    if sys.platform.startswith("linux") and shutil.which("xsel"):
        try:
            res = subprocess.run(["xsel", "-b", "-o"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass

    # 5. macOS pbpaste
    if sys.platform == "darwin" and shutil.which("pbpaste"):
        try:
            res = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass

    # 6. Fallback to Textual in-app clipboard if available
    if app is not None and hasattr(app, "clipboard"):
        clip_val = getattr(app, "clipboard", "")
        if clip_val:
            return str(clip_val)

    return ""


def save_clipboard_image(
    image: Any,
    session_id: Optional[str] = None,
    index: int = 1,
    base_dir: Optional[Path] = None,
) -> Path:
    """Save pasted PIL image to ~/.rackhand/images/ and return its path."""
    dest_dir = base_dir or (Path.home() / ".rackhand" / "images")
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sess_part = f"sess_{session_id}_" if session_id else ""
    filename = f"pasted_{sess_part}{ts}_{index}.png"
    dest_path = dest_dir / filename

    image.save(dest_path, format="PNG")

    try:
        from vault import get_vault
        v = get_vault()
        if v.is_unlocked():
            import io
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            v.save_image(filename, buf.getvalue())
    except Exception:
        pass

    return dest_path
