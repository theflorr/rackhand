"""Comprehensive tests for the encrypted Vault system."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path

import pytest
import zxcvbn
import nacl.pwhash.argon2id as argon2id

from vault import (
    DecryptionError,
    InvalidPassphraseError,
    MigrationVerificationError,
    TamperedDataError,
    Vault,
    VaultLockedError,
    normalize_passphrase,
)

# Use interactive limits in tests to keep test runs fast (< 100ms)
FAST_OPS = argon2id.OPSLIMIT_INTERACTIVE
FAST_MEM = argon2id.MEMLIMIT_INTERACTIVE


@pytest.fixture
def temp_vault_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_passphrase_normalization_nfkc():
    # 'é' as single character vs 'e' + combining acute accent
    composed = "café-secürity-2026!"
    decomposed = "cafe\u0301-secu\u0308rity-2026!"
    assert composed != decomposed
    assert normalize_passphrase(composed) == normalize_passphrase(decomposed)


def test_zxcvbn_strength_validation():
    from ui.modals.vault import safe_zxcvbn

    # 0. Empty string (e.g. typing then deleting) must not crash with IndexError
    res_empty = safe_zxcvbn("")
    assert res_empty["score"] == 0

    # 1. Under 12 characters should fail length requirement
    res_short = safe_zxcvbn("Short1!")
    assert len("Short1!") < 12

    # 2. 12+ characters but predictable / weak dictionary phrase (score < 3)
    res_weak = safe_zxcvbn("password1234567")
    assert len("password1234567") >= 12
    assert res_weak["score"] < 3

    # 3. Strong passphrase (len >= 12, score >= 3)
    strong_pw = "Correct-Horse-Battery-Staple-2026!"
    res_strong = safe_zxcvbn(strong_pw)
    assert len(strong_pw) >= 12
    assert res_strong["score"] >= 3


def test_vault_round_trip(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)

    assert not vault.is_vault_initialized()
    assert not vault.is_unlocked()

    passphrase = "Master-Vault-Secret-Passphrase-2026!"
    vault.create_vault(passphrase, opslimit=FAST_OPS, memlimit=FAST_MEM)

    assert vault.is_vault_initialized()
    assert vault.is_unlocked()

    # Store all types of data
    vault.set_settings({"theme": "dark", "approval_mode": "auto_safe"})
    vault.set_provider("openrouter", {"name": "OpenRouter", "api_key": "sk-or-test1234"})
    vault.set_profiles([{"id": "p1", "name": "Switch1", "vendor": "cisco_ios", "port": "COM3"}])
    vault.set_sessions_index([{"id": "sess_001", "num": 1, "title": "BGP Debugging"}])
    vault.set_session_details("sess_001", {"id": "sess_001", "num": 1}, [{"role": "user", "text": "show ip bgp"}], [{"role": "user", "content": "show ip bgp"}])
    vault.save_snapshot("snap_01", "Switch1", "1700000000", "hostname Switch1\ninterface Gi0/1\n", {"id": "snap_01"})
    vault.append_transcript("sess_001", "BGP_Debugging", "### USER\nshow ip bgp")
    vault.save_image("diagram.png", b"\x89PNG\r\n\x1a\nTESTDATA")
    vault.save_usage([{"timestamp": 12345.0, "provider_key": "openrouter", "model": "m1", "tokens_in": 10, "tokens_out": 20}], {"openrouter": {"limit": 50}}, {"openrouter": {"quota": 100}})

    # Close and lock vault
    vault.close()
    assert not vault.is_unlocked()
    with pytest.raises(VaultLockedError):
        vault.get_settings()

    # Re-open and unlock with correct passphrase
    vault2 = Vault(vpath)
    vault2.unlock(passphrase)
    assert vault2.is_unlocked()

    # Verify all data matches exactly
    assert vault2.get_settings() == {"theme": "dark", "approval_mode": "auto_safe"}
    provs = vault2.get_providers()
    assert provs["openrouter"]["api_key"] == "sk-or-test1234"
    profs = vault2.get_profiles()
    assert profs[0]["name"] == "Switch1"
    s_idx = vault2.get_sessions_index()
    assert s_idx[0]["id"] == "sess_001"
    ui_m, mem_m = vault2.get_session_details("sess_001")
    assert ui_m[0]["text"] == "show ip bgp"
    assert mem_m[0]["content"] == "show ip bgp"
    snaps = vault2.get_snapshots("Switch1")
    assert len(snaps) == 1
    assert "hostname Switch1" in snaps[0][1]
    trans = vault2.get_transcripts("sess_001")
    assert "### USER" in trans[0]
    assert vault2.get_image("diagram.png") == b"\x89PNG\r\n\x1a\nTESTDATA"
    entries, rl, q = vault2.load_usage()
    assert len(entries) == 1
    assert rl["openrouter"]["limit"] == 50
    assert q["openrouter"]["quota"] == 100


def test_vault_wrong_passphrase(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)
    correct_pw = "Correct-Master-Passphrase-2026!"
    wrong_pw = "Wrong-Master-Passphrase-2026!"

    vault.create_vault(correct_pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.set_settings({"secret": "top_secret_data"})
    vault.close()

    # Unlock with wrong passphrase
    vault2 = Vault(vpath)
    with pytest.raises(InvalidPassphraseError):
        vault2.unlock(wrong_pw)

    assert not vault2.is_unlocked()
    with pytest.raises(VaultLockedError):
        vault2.get_settings()


def test_vault_tampered_header_and_data(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)
    pw = "Tamper-Test-Passphrase-2026!"

    vault.create_vault(pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.set_settings({"foo": "bar"})
    vault.close()

    raw_bytes = bytearray(vpath.read_bytes())

    # 1. Tamper magic bytes
    tampered_magic = bytearray(raw_bytes)
    tampered_magic[0] = ord(b"X")
    vpath.write_bytes(bytes(tampered_magic))
    with pytest.raises(TamperedDataError):
        Vault(vpath).unlock(pw)

    # 2. Tamper version byte
    tampered_ver = bytearray(raw_bytes)
    tampered_ver[4] = 99
    vpath.write_bytes(bytes(tampered_ver))
    with pytest.raises(TamperedDataError):
        Vault(vpath).unlock(pw)

    # 3. Tamper ciphertext payload (AEAD Poly1305 MAC failure)
    tampered_ct = bytearray(raw_bytes)
    tampered_ct[-8] ^= 0xAA
    vpath.write_bytes(bytes(tampered_ct))
    with pytest.raises(TamperedDataError):
        Vault(vpath).unlock(pw)


def test_vault_migration_verification_and_deletion(temp_vault_dir):
    # Setup legacy files in temp_vault_dir
    legacy_dir = temp_vault_dir
    (legacy_dir / "settings.json").write_text(json.dumps({"theme": "dark", "approval_mode": "ask_changes"}))
    (legacy_dir / "providers.json").write_text(json.dumps({"p1": {"name": "Anthropic", "api_key": "sk-ant-test"}}))
    (legacy_dir / "profiles.json").write_text(json.dumps([{"id": "prof1", "name": "Core-Router", "vendor": "cisco_ios"}]))

    sess_dir = legacy_dir / "sessions"
    sess_dir.mkdir()
    (sess_dir / "session_1.json").write_text(json.dumps({
        "session": {"id": "session_1", "num": 1, "title": "OSPF Troubleshooting"},
        "ui_messages": [{"role": "user", "text": "show ip ospf neighbor"}],
        "mem_messages": [{"role": "user", "content": "show ip ospf neighbor"}]
    }))
    (legacy_dir / "sessions.json").write_text(json.dumps([{"id": "session_1", "num": 1, "title": "OSPF Troubleshooting"}]))

    snap_dir = legacy_dir / "snapshots" / "Core-Router"
    snap_dir.mkdir(parents=True)
    (snap_dir / "snap_01_20261124.cfg").write_text("hostname Core-Router\nrouter ospf 1\n")

    trans_dir = legacy_dir / "transcripts"
    trans_dir.mkdir(parents=True)
    (trans_dir / "OSPF_Troubleshooting_session_1.md").write_text("### USER\nshow ip ospf neighbor\n")

    img_dir = legacy_dir / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "topo.png").write_bytes(b"\x89PNG_TOPOLOGY")

    (legacy_dir / "usage_history.json").write_text(json.dumps({
        "entries": [{"timestamp": 999.0, "provider_key": "p1", "model": "m1", "tokens_in": 5, "tokens_out": 15, "session_id": "session_1"}],
        "rate_limits": {"p1": {"limit": 10}},
        "api_quotas": {"p1": {"quota": 20}}
    }))

    vpath = legacy_dir / "vault.rhv"
    vault = Vault(vpath)
    assert vault.has_unencrypted_data(legacy_dir)

    pw = "Migrate-Verification-Passphrase-2026!"
    vault.create_vault(pw, opslimit=FAST_OPS, memlimit=FAST_MEM)

    # Run migration with automated verification
    res = vault.migrate_legacy_data(legacy_dir=legacy_dir)
    assert res.success is True
    assert res.settings_count == 1
    assert res.providers_count == 1
    assert res.profiles_count == 1
    assert res.sessions_count == 1
    assert res.snapshots_count == 1
    assert res.transcripts_count == 1
    assert res.images_count == 1
    assert res.usage_entries_count == 1

    # Verify that unencrypted originals have been deleted
    assert not (legacy_dir / "settings.json").exists()
    assert not (legacy_dir / "providers.json").exists()
    assert not (legacy_dir / "profiles.json").exists()
    assert not (legacy_dir / "sessions.json").exists()
    assert not (legacy_dir / "sessions").exists()
    assert not (legacy_dir / "snapshots").exists()
    assert not (legacy_dir / "transcripts").exists()
    assert not (legacy_dir / "images").exists()
    assert not (legacy_dir / "usage_history.json").exists()
    assert not vault.has_unencrypted_data(legacy_dir)

    # Close and re-open vault to verify all data is intact
    vault.close()
    vault2 = Vault(vpath)
    vault2.unlock(pw)
    assert vault2.get_settings()["theme"] == "dark"
    assert vault2.get_providers()["p1"]["api_key"] == "sk-ant-test"
    assert vault2.get_profiles()[0]["name"] == "Core-Router"
    assert vault2.get_sessions_index()[0]["id"] == "session_1"
    ui_msgs, mem_msgs = vault2.get_session_details("session_1")
    assert ui_msgs[0]["text"] == "show ip ospf neighbor"
    assert "hostname Core-Router" in vault2.get_snapshots("Core-Router")[0][1]
    assert "show ip ospf" in vault2.get_transcripts("session_1")[0]
    assert vault2.get_image("topo.png") == b"\x89PNG_TOPOLOGY"


def test_vault_change_passphrase(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)

    old_pw = "Original-Master-Passphrase-2026!"
    new_pw = "Updated-Master-Passphrase-2026!"

    vault.create_vault(old_pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.set_settings({"audit_key": "active_data"})
    vault.set_provider("prov1", {"name": "TestProv", "api_key": "initial_key"})

    # Incorrect current passphrase should fail
    with pytest.raises(InvalidPassphraseError):
        vault.change_passphrase("Wrong-Current-PW", new_pw, opslimit=FAST_OPS, memlimit=FAST_MEM)

    # Valid change passphrase (re-wraps data key)
    vault.change_passphrase(old_pw, new_pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.close()

    # Old passphrase must fail
    vault_old = Vault(vpath)
    with pytest.raises(InvalidPassphraseError):
        vault_old.unlock(old_pw)

    # New passphrase must succeed and retain all data
    vault_new = Vault(vpath)
    vault_new.unlock(new_pw)
    assert vault_new.get_settings() == {"audit_key": "active_data"}
    assert vault_new.get_providers()["prov1"]["api_key"] == "initial_key"


def test_vault_atomic_write_and_backup(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)
    pw = "Atomic-Write-Passphrase-2026!"

    vault.create_vault(pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.set_settings({"version": 1})
    assert vpath.is_file()
    assert not vault.temp_path.is_file()

    # Save a second time - backup file must be created
    vault.set_settings({"version": 2})
    assert vault.backup_path.is_file()

    # Verify backup is a valid encrypted vault file
    vault_bak = Vault(vault.backup_path)
    vault_bak.unlock(pw)
    assert vault_bak.get_settings() == {"version": 1}


def test_vault_wipe_and_start_fresh(temp_vault_dir):
    vpath = temp_vault_dir / "vault.rhv"
    vault = Vault(vpath)
    pw = "Wipe-Test-Passphrase-2026!"

    vault.create_vault(pw, opslimit=FAST_OPS, memlimit=FAST_MEM)
    vault.set_settings({"data": "critical"})
    vault.set_settings({"data": "critical_v2"})  # creates backup
    assert vpath.is_file()
    assert vault.backup_path.is_file()

    vault.wipe()
    assert not vpath.exists()
    assert not vault.backup_path.exists()
    assert not vault.is_unlocked()
    assert not vault.is_vault_initialized()


@pytest.mark.asyncio
async def test_vault_setup_checkbox_no_ellipsis():
    from textual.app import App
    from textual.widgets import Checkbox
    from ui.modals.vault import VaultSetupModal

    class _TestApp(App):
        def on_mount(self) -> None:
            self.push_screen(VaultSetupModal())

    app = _TestApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        cb = app.screen.query_one("#setup-checkbox", Checkbox)
        assert cb.render_line(0).text == "▐X▌"
        cb.value = True
        await pilot.pause()
        assert cb.render_line(0).text == "▐X▌"
