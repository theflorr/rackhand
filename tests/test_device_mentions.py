import pytest
from unittest.mock import MagicMock
from textual.app import App
from models import DeviceProfile, DeviceSession, SSHSettings, Transport, Vendor
from ui.suggestions import get_suggestions, get_device_suggestions, CommandSuggestion, PromptInput


def test_get_device_suggestions_connected_and_saved():
    app = MagicMock()
    prof1 = DeviceProfile(name="SW1", transport=Transport.SSH, vendor=Vendor.CISCO_IOS, ssh=SSHSettings(host="192.168.1.1", port=22))
    sess1 = DeviceSession(id="sess-1", profile=prof1, hostname="SW1")

    prof2 = DeviceProfile(name="Core-R1", transport=Transport.SSH, vendor=Vendor.JUNIPER_JUNOS, ssh=SSHSettings(host="10.0.0.1", port=22))
    sess2 = DeviceSession(id="sess-2", profile=prof2, hostname="Core-R1")

    app.sessions = [sess1, sess2]
    app.active_id = "sess-1"

    prof3 = DeviceProfile(name="Edge-FW", transport=Transport.SSH, vendor=Vendor.FORTIOS, ssh=SSHSettings(host="172.16.0.1", port=22))
    app.backend.get_profiles_sync.return_value = [prof1, prof2, prof3]

    # 1. Query for all devices (@)
    suggestions = get_device_suggestions("", app)
    names = [s.value for s in suggestions]
    assert "@SW1" in names
    assert "@Core-R1" in names
    assert "@Edge-FW" in names

    # Verify SW1 is marked active and Edge-FW is marked saved
    sw1 = next(s for s in suggestions if s.value == "@SW1")
    assert "Active" in sw1.description
    assert sw1.category == "Device"

    fw = next(s for s in suggestions if s.value == "@Edge-FW")
    assert "Saved" in fw.description

    # 2. Query with filter (@SW)
    sw_suggestions = get_device_suggestions("SW", app)
    assert len(sw_suggestions) == 1
    assert sw_suggestions[0].value == "@SW1"


def test_get_suggestions_mention_trigger():
    app = MagicMock()
    prof1 = DeviceProfile(name="SW1", transport=Transport.SSH, vendor=Vendor.CISCO_IOS, ssh=SSHSettings(host="192.168.1.1", port=22))
    sess1 = DeviceSession(id="sess-1", profile=prof1, hostname="SW1")
    app.sessions = [sess1]
    app.active_id = "sess-1"
    app.backend.get_profiles_sync.return_value = []

    # 1. Just @
    res = get_suggestions("@", app, cursor_pos=1)
    assert len(res) == 1
    assert res[0].value == "@SW1"

    # 2. In middle of sentence: "What is IP on @S"
    prompt = "What is IP on @S"
    res2 = get_suggestions(prompt, app, cursor_pos=len(prompt))
    assert len(res2) == 1
    assert res2[0].value == "@SW1"

    # 3. After space after mention: "@SW1 What's the IP" (cursor at end)
    prompt3 = "@SW1 What's the IP"
    res3 = get_suggestions(prompt3, app, cursor_pos=len(prompt3))
    assert res3 == []


@pytest.mark.asyncio
async def test_apply_completion_device_mention():
    class TestApp(App):
        def compose(self):
            yield PromptInput(id="prompt")

    test_app = TestApp()
    async with test_app.run_test() as pilot:
        p_inp = test_app.query_one("#prompt", PromptInput)
        p_inp.value = "@SW"
        p_inp.cursor_position = 3
        sel = CommandSuggestion(value="@SW1", display="@SW1", description="Cisco", category="Device", completion="@SW1 ")

        p_inp.apply_completion(sel)
        assert p_inp.value == "@SW1 "
        assert p_inp.cursor_position == 5

        # Test in middle of sentence
        p_inp.value = "Check config on @s for vlan 1"
        p_inp.cursor_position = 18  # right after @s
        p_inp.apply_completion(sel)
        assert p_inp.value == "Check config on @SW1 for vlan 1"
        assert p_inp.cursor_position == len("Check config on @SW1 ")

