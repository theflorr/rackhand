from __future__ import annotations

import tempfile
from pathlib import Path
from PIL import Image
import pytest

from ui.clipboard import (
    PastedTextBlock,
    clear_test_clipboard,
    get_system_clipboard_image,
    get_system_clipboard_text,
    save_clipboard_image,
    set_test_clipboard,
)
from ui.messages import UserMessage, deserialize_message, serialize_message
from ui.suggestions import PromptInput


@pytest.fixture(autouse=True)
def clean_clipboard():
    from textual.app import App
    from textual._context import active_app

    class DummyApp(App):
        def clear_selection(self) -> None:
            pass

    app = DummyApp()
    token = active_app.set(app)
    clear_test_clipboard()
    yield app
    clear_test_clipboard()
    active_app.reset(token)


def test_clipboard_text_under_45_chars():
    inp = PromptInput()
    # 24 characters (under 45 characters)
    inp._handle_pasted_text("show ip interface brief")
    assert inp.value == "show ip interface brief"
    assert len(inp._pasted_blocks) == 0


def test_clipboard_text_over_45_chars_triggers_block():
    inp = PromptInput()
    # 63 characters (over 45 characters)
    text = "show running-config interface GigabitEthernet0/0/0/1.100 detail"
    assert len(text) == 63
    assert len(text) > 45

    inp._handle_pasted_text(text)
    assert inp.value == "[63 characters]"
    assert len(inp._pasted_blocks) == 1
    assert inp._pasted_blocks[0].tag == "[63 characters]"
    assert inp._pasted_blocks[0].content == text
    assert inp._pasted_blocks[0].line_count == 63


def test_clipboard_text_932_chars():
    inp = PromptInput()
    text = "x" * 932
    assert len(text) == 932

    inp._handle_pasted_text(text)
    assert inp.value == "[932 characters]"
    assert len(inp._pasted_blocks) == 1
    assert inp._pasted_blocks[0].tag == "[932 characters]"
    assert inp._pasted_blocks[0].content == text


def test_paste_event_prevents_duplicate_full_text():
    """Verify that terminal bracketed paste does NOT duplicate raw text alongside []."""
    from textual.events import Paste
    inp = PromptInput()
    raw = "router bgp 65001\n neighbor 10.1.1.1 remote-as 65002\n neighbor 10.1.1.1 activate"
    p = Paste(raw)
    inp._on_paste(p)

    # Should ONLY be [N characters], without the raw text being appended or prepended
    expected_tag = f"[{len(raw)} characters]"
    assert inp.value == expected_tag
    assert "router bgp" not in inp.value
    assert p._no_default_action is True
    assert p._stop_propagation is True


def test_resolve_multiple_pasted_blocks():
    inp = PromptInput()
    block1 = "configure terminal\ninterface GigabitEthernet0/1\nip address 10.0.0.1 255.255.255.0"
    block2 = "ip route 10.0.0.0 255.0.0.0 192.168.1.1\nip route 172.16.0.0 255.240.0.0 192.168.1.2\nip route 0.0.0.0 0.0.0.0 192.168.1.254\n"

    inp._handle_pasted_text(block1)
    inp._insert_token_or_text(" and also ")
    inp._handle_pasted_text(block2)

    assert f"[{len(block1)} characters]" in inp.value
    assert "and also" in inp.value
    assert f"[{len(block2)} characters]" in inp.value

    display_text, expanded_text, attached_images = inp.resolve_for_submission(inp.value)

    assert display_text == inp.value
    assert block1 in expanded_text
    assert "ip route 10.0.0.0" in expanded_text
    assert f"[{len(block1)} characters]" not in expanded_text


def test_image_paste_creates_image_token(tmp_path):
    img = Image.new("RGB", (64, 64), color="blue")
    set_test_clipboard(image=img)

    inp = PromptInput()
    inp.action_paste()

    assert inp.value == "[IMAGE #1]"
    assert len(inp._pasted_images) == 1
    assert "[IMAGE #1]" in inp._pasted_images
    img_path = Path(inp._pasted_images["[IMAGE #1]"])
    assert img_path.exists()
    assert img_path.suffix == ".png"

    # Second image paste
    img2 = Image.new("RGB", (32, 32), color="green")
    set_test_clipboard(image=img2)
    inp.action_paste()

    assert inp.value == "[IMAGE #1][IMAGE #2]"
    assert "[IMAGE #2]" in inp._pasted_images


def test_raw_image_path_is_not_treated_as_image():
    """Raw image paths should not work, only pasted images."""
    inp = PromptInput()
    # Pasting plain text under 45 chars
    inp._handle_pasted_text("/home/user/images/pic.png")
    assert inp.value == "/home/user/images/pic.png"
    assert len(inp._pasted_images) == 0

    display, expanded, images = inp.resolve_for_submission(inp.value)
    assert display == "/home/user/images/pic.png"
    assert expanded == "/home/user/images/pic.png"
    assert len(images) == 0


def test_submission_resolution_with_image_and_chars():
    img = Image.new("RGB", (100, 100), color="red")
    set_test_clipboard(image=img)

    inp = PromptInput()
    inp._insert_token_or_text("Please review ")
    inp.action_paste()  # Inserts [IMAGE #1]
    inp._insert_token_or_text(" and config: ")

    config_text = "router bgp 65001\n bgp router-id 1.1.1.1\n neighbor 10.0.0.2 remote-as 65002\n neighbor 10.0.0.2 activate"
    assert len(config_text) > 45
    inp._handle_pasted_text(config_text)

    expected_tag = f"[{len(config_text)} characters]"
    assert inp.value == f"Please review [IMAGE #1] and config: {expected_tag}"

    display_text, expanded_text, attached_images = inp.resolve_for_submission(inp.value)

    assert display_text == f"Please review [IMAGE #1] and config: {expected_tag}"
    assert "[IMAGE #1]" in display_text
    assert expected_tag in display_text
    assert config_text in expanded_text
    assert expected_tag not in expanded_text
    assert "[IMAGE #1]" in attached_images

    # Clear pasted data
    inp.clear_pasted_data()
    assert len(inp._pasted_blocks) == 0
    assert len(inp._pasted_images) == 0
    assert inp._image_counter == 0


def test_user_message_badge_styling():
    msg = UserMessage("Look at [IMAGE #1] and [932 characters]")
    rendered = msg.render()
    plain = rendered.plain

    assert "Look at [IMAGE #1] and [932 characters]" in plain
    assert len(rendered.spans) >= 2


def test_user_message_serialization_with_attachments():
    msg = UserMessage("Review [IMAGE #1]", attachments={"images": {"[IMAGE #1]": "/tmp/test.png"}})
    d = serialize_message(msg)
    assert d["type"] == "user"
    assert d["text"] == "Review [IMAGE #1]"
    assert d["attachments"]["images"]["[IMAGE #1]"] == "/tmp/test.png"

    restored = deserialize_message(d)
    assert isinstance(restored, UserMessage)
    assert restored.text == "Review [IMAGE #1]"
    assert restored.attachments["images"]["[IMAGE #1]"] == "/tmp/test.png"
