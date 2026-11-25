"""Single Encrypted Vault Module for Rackhand.

Provides authenticated local encryption for all Rackhand user data using:
- Argon2id KDF (libsodium MODERATE preset by default, via PyNaCl)
- XChaCha20-Poly1305 authenticated symmetric encryption with 24-byte nonces
- Authenticated file header as Associated Data (AAD)
- NFKC passphrase normalization
- In-memory SQLite database loaded via deserialize() and saved via serialize()
- Atomic write with backup file
- Serialized writes through one thread/asyncio locked Vault object
- Plaintext data migration with read-back verification and deletion of originals
- Passphrase re-wrapping
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import nacl.bindings as nb
import nacl.exceptions
import nacl.pwhash.argon2id as argon2id
import nacl.utils

logger = logging.getLogger("rackhand.vault")

MAGIC = b"RHV1"
VERSION = 1

# Header layout:
# 4 bytes: Magic ("RHV1")
# 1 byte:  Version (1)
# 16 bytes: Salt (Argon2id salt)
# 4 bytes: Opslimit (uint32)
# 8 bytes: Memlimit (uint64)
# 24 bytes: Wrap Nonce (XChaCha20-Poly1305 nonce for wrapping the 32-byte data key)
# 48 bytes: Wrapped Data Key (32-byte key + 16-byte Poly1305 MAC tag)
HEADER_STRUCT = ">4sB16sIQ24s48s"
HEADER_LEN = struct.calcsize(HEADER_STRUCT)  # 105 bytes
NONCE_LEN = 24  # 24 bytes data nonce for payload encryption
KEY_LEN = 32    # 32 bytes data key
WRAP_AAD = b"RACKHAND_KEY_WRAP"


class VaultError(Exception):
    """Base exception for vault errors."""


class DecryptionError(VaultError):
    """Failed to decrypt vault data."""


class InvalidPassphraseError(DecryptionError):
    """Supplied passphrase failed authentication/decryption."""


class TamperedDataError(DecryptionError):
    """Vault file has been modified or corrupted (MAC verification failure)."""


class VaultLockedError(VaultError):
    """Attempted to access vault while locked."""


class MigrationVerificationError(VaultError):
    """Migrated vault data failed read-back verification."""


def normalize_passphrase(passphrase: str) -> bytes:
    """Normalize master passphrase using Unicode NFKC normalization."""
    if not isinstance(passphrase, str):
        passphrase = str(passphrase or "")
    return unicodedata.normalize("NFKC", passphrase).encode("utf-8")


@dataclass
class MigrationResult:
    success: bool
    settings_count: int = 0
    providers_count: int = 0
    profiles_count: int = 0
    sessions_count: int = 0
    snapshots_count: int = 0
    transcripts_count: int = 0
    images_count: int = 0
    usage_entries_count: int = 0
    deleted_files: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.deleted_files is None:
            self.deleted_files = []


class Vault:
    """Singleton-friendly encrypted vault managing in-memory SQLite and encrypted disk persistence."""

    _instance: Optional[Vault] = None

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or (Path.home() / ".rackhand" / "vault.rhv")).resolve()
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.temp_path = self.path.with_name(self.path.name + ".tmp")
        self.base_dir = self.path.parent

        self._sync_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

        self._data_key: Optional[bytes] = None
        self._header_bytes: Optional[bytes] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._unlocked: bool = False

    @property
    def async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    @classmethod
    def get_instance(cls, path: Optional[Path] = None) -> Vault:
        if cls._instance is None:
            cls._instance = Vault(path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        if cls._instance is not None:
            try:
                cls._instance.close()
            except Exception:
                pass
            cls._instance = None

    def is_vault_initialized(self) -> bool:
        """Return True if the encrypted vault file exists on disk."""
        return self.path.is_file() and self.path.stat().st_size >= (HEADER_LEN + NONCE_LEN + 16)

    def is_unlocked(self) -> bool:
        """Return True if vault is currently unlocked and in-memory DB is accessible."""
        with self._sync_lock:
            return self._unlocked and self._conn is not None and self._data_key is not None

    def has_unencrypted_data(self, legacy_dir: Optional[Path] = None) -> bool:
        """Check if any unencrypted user data files exist in the rackhand directory."""
        bdir = Path(legacy_dir or self.base_dir)
        if not bdir.is_dir():
            return False

        checks = [
            bdir / "settings.json",
            bdir / "providers.json",
            bdir / "profiles.json",
            bdir / "sessions.json",
            bdir / "usage_history.json",
        ]
        for c in checks:
            if c.is_file() and c.stat().st_size > 2:
                return True

        dir_checks = [
            bdir / "sessions",
            bdir / "snapshots",
            bdir / "transcripts",
            bdir / "images",
        ]
        for d in dir_checks:
            if d.is_dir():
                for _ in d.rglob("*"):
                    return True
        return False

    # ---- lifecycle & crypto -------------------------------------------------

    def create_vault(
        self,
        passphrase: str,
        opslimit: Optional[int] = None,
        memlimit: Optional[int] = None,
    ) -> None:
        """Create a new encrypted vault file with a fresh random 32-byte data key and SQLite DB."""
        with self._sync_lock:
            norm_pw = normalize_passphrase(passphrase)
            if not norm_pw:
                raise ValueError("Passphrase cannot be empty")

            ops = int(opslimit if opslimit is not None else argon2id.OPSLIMIT_MODERATE)
            mem = int(memlimit if memlimit is not None else argon2id.MEMLIMIT_MODERATE)
            salt = nacl.utils.random(argon2id.SALTBYTES)

            # 1. Derive wrapping key from passphrase via Argon2id
            derived_key = argon2id.kdf(KEY_LEN, norm_pw, salt, opslimit=ops, memlimit=mem)

            # 2. Generate random 32-byte data key
            data_key = nacl.utils.random(KEY_LEN)

            # 3. Wrap data key using XChaCha20-Poly1305
            wrap_nonce = nacl.utils.random(NONCE_LEN)
            wrapped_key = nb.crypto_aead_xchacha20poly1305_ietf_encrypt(
                data_key, WRAP_AAD, wrap_nonce, derived_key
            )

            # 4. Pack header
            header_bytes = struct.pack(
                HEADER_STRUCT,
                MAGIC,
                VERSION,
                salt,
                ops,
                mem,
                wrap_nonce,
                wrapped_key,
            )

            # 5. Initialize in-memory SQLite database
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._init_db_schema(conn)

            self._data_key = data_key
            self._header_bytes = header_bytes
            self._conn = conn
            self._unlocked = True

            # 6. Save initial empty database to disk
            self.save()

    def unlock(self, passphrase: str) -> None:
        """Unlock existing vault with master passphrase and load SQLite into memory."""
        with self._sync_lock:
            if not self.path.is_file():
                raise VaultError(f"Vault file not found at {self.path}")

            file_bytes = self.path.read_bytes()
            if len(file_bytes) < (HEADER_LEN + NONCE_LEN + 16):
                raise TamperedDataError("Vault file is truncated or corrupted")

            header_bytes = file_bytes[:HEADER_LEN]
            magic, ver, salt, ops, mem, wrap_nonce, wrapped_key = struct.unpack(
                HEADER_STRUCT, header_bytes
            )

            if magic != MAGIC:
                raise TamperedDataError(f"Invalid vault magic header: {magic!r}")
            if ver != VERSION:
                raise TamperedDataError(f"Unsupported vault version: {ver}")

            norm_pw = normalize_passphrase(passphrase)

            # 1. Derive key via Argon2id using header params
            try:
                derived_key = argon2id.kdf(KEY_LEN, norm_pw, salt, opslimit=ops, memlimit=mem)
            except Exception as exc:
                raise DecryptionError(f"KDF derivation failed: {exc}") from exc

            # 2. Unwrap data key
            try:
                data_key = nb.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    wrapped_key, WRAP_AAD, wrap_nonce, derived_key
                )
            except nacl.exceptions.CryptoError:
                raise InvalidPassphraseError("Incorrect passphrase") from None
            except Exception as exc:
                raise DecryptionError(f"Key unwrapping failed: {exc}") from exc

            # 3. Decrypt database payload
            data_nonce = file_bytes[HEADER_LEN : HEADER_LEN + NONCE_LEN]
            ciphertext = file_bytes[HEADER_LEN + NONCE_LEN :]

            try:
                # File header is authenticated as associated data (AAD)
                plaintext_db = nb.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    ciphertext, header_bytes, data_nonce, data_key
                )
            except nacl.exceptions.CryptoError:
                raise TamperedDataError("Decryption failed: vault data or header has been tampered with") from None
            except Exception as exc:
                raise DecryptionError(f"Payload decryption failed: {exc}") from exc

            # 4. Load SQLite database via deserialize()
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                conn.deserialize(plaintext_db)
            except Exception as exc:
                raise TamperedDataError(f"Corrupt SQLite database in vault payload: {exc}") from exc

            self._data_key = data_key
            self._header_bytes = header_bytes
            self._conn = conn
            self._unlocked = True

    def save(self) -> None:
        """Serialize in-memory SQLite, encrypt with XChaCha20-Poly1305, and write atomically."""
        with self._sync_lock:
            if not self._unlocked or self._conn is None or self._data_key is None or self._header_bytes is None:
                raise VaultLockedError("Cannot save locked or uninitialized vault")

            self._conn.commit()
            db_bytes = self._conn.serialize()

            # Fresh 24-byte nonce per write
            data_nonce = nacl.utils.random(NONCE_LEN)

            # Encrypt database with data key and authenticate header as AAD
            ciphertext = nb.crypto_aead_xchacha20poly1305_ietf_encrypt(
                db_bytes, self._header_bytes, data_nonce, self._data_key
            )

            full_payload = self._header_bytes + data_nonce + ciphertext

            self.base_dir.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    os.chmod(self.base_dir, 0o700)
                except OSError:
                    pass

            # Atomic write via temp file then rename with backup
            with open(self.temp_path, "wb") as f:
                f.write(full_payload)
                f.flush()
                os.fsync(f.fileno())
            if os.name != "nt":
                try:
                    os.chmod(self.temp_path, 0o600)
                except OSError:
                    pass

            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.backup_path)
                    if os.name != "nt":
                        os.chmod(self.backup_path, 0o600)
                except Exception as exc:
                    logger.debug("Failed creating vault backup: %s", exc)

            os.replace(self.temp_path, self.path)

    async def async_save(self) -> None:
        """Async-safe save serializing through asyncio lock."""
        async with self.async_lock:
            await asyncio.to_thread(self.save)

    def change_passphrase(
        self,
        current_passphrase: str,
        new_passphrase: str,
        opslimit: Optional[int] = None,
        memlimit: Optional[int] = None,
    ) -> None:
        """Re-wrap the data key with a new Argon2id key derived from new passphrase."""
        with self._sync_lock:
            if not self._unlocked or self._data_key is None or self._header_bytes is None:
                raise VaultLockedError("Vault must be unlocked to change passphrase")

            # 1. Verify current passphrase
            magic, ver, salt, ops, mem, wrap_nonce, wrapped_key = struct.unpack(
                HEADER_STRUCT, self._header_bytes
            )
            norm_curr = normalize_passphrase(current_passphrase)
            try:
                curr_dk = argon2id.kdf(KEY_LEN, norm_curr, salt, opslimit=ops, memlimit=mem)
                unwrapped = nb.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    wrapped_key, WRAP_AAD, wrap_nonce, curr_dk
                )
                if unwrapped != self._data_key:
                    raise InvalidPassphraseError("Current passphrase verification failed")
            except Exception:
                raise InvalidPassphraseError("Current passphrase is incorrect") from None

            # 2. Derive new key with fresh salt
            norm_new = normalize_passphrase(new_passphrase)
            if not norm_new:
                raise ValueError("New passphrase cannot be empty")

            new_ops = int(opslimit if opslimit is not None else argon2id.OPSLIMIT_MODERATE)
            new_mem = int(memlimit if memlimit is not None else argon2id.MEMLIMIT_MODERATE)
            new_salt = nacl.utils.random(argon2id.SALTBYTES)
            new_dk = argon2id.kdf(KEY_LEN, norm_new, new_salt, opslimit=new_ops, memlimit=new_mem)

            # 3. Re-wrap the EXISTING data key with new key
            new_wrap_nonce = nacl.utils.random(NONCE_LEN)
            new_wrapped_key = nb.crypto_aead_xchacha20poly1305_ietf_encrypt(
                self._data_key, WRAP_AAD, new_wrap_nonce, new_dk
            )

            # 4. Form updated header
            new_header_bytes = struct.pack(
                HEADER_STRUCT,
                MAGIC,
                VERSION,
                new_salt,
                new_ops,
                new_mem,
                new_wrap_nonce,
                new_wrapped_key,
            )

            old_header_bytes = self._header_bytes
            self._header_bytes = new_header_bytes
            try:
                self.save()
            except Exception:
                self._header_bytes = old_header_bytes
                raise

    def wipe(self) -> None:
        """Permanently wipe vault files and clear in-memory keys."""
        with self._sync_lock:
            self.close()
            for p in (self.path, self.backup_path, self.temp_path):
                try:
                    if p.is_file():
                        p.unlink()
                except Exception:
                    pass

    def close(self) -> None:
        """Close database connection and scrub in-memory encryption keys."""
        with self._sync_lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._data_key = None
            self._header_bytes = None
            self._unlocked = False

    # ---- schema & data migration --------------------------------------------

    def _init_db_schema(self, conn: sqlite3.Connection) -> None:
        c = conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            id TEXT PRIMARY KEY,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS providers (
            key TEXT PRIMARY KEY,
            name TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            num INTEGER,
            title TEXT,
            data TEXT,
            ui_messages TEXT,
            mem_messages TEXT,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            hostname TEXT,
            timestamp_str TEXT,
            config_text TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            safe_title TEXT,
            timestamp REAL,
            entry TEXT
        );
        CREATE TABLE IF NOT EXISTS images (
            filename TEXT PRIMARY KEY,
            data BLOB,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            provider_key TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS usage_meta (
            key TEXT PRIMARY KEY,
            data TEXT
        );
        """)
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("version", "1"))
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("created_at", str(time.time())))
        conn.commit()

    def migrate_legacy_data(self, legacy_dir: Optional[Path] = None) -> MigrationResult:
        """Migrate all legacy plaintext data into the vault, verify read-back, then delete originals."""
        with self._sync_lock:
            if not self.is_unlocked() or self._conn is None:
                raise VaultLockedError("Vault must be unlocked before migration")

            bdir = Path(legacy_dir or self.base_dir)
            result = MigrationResult(success=False)
            files_to_delete: list[Path] = []
            dirs_to_delete: list[Path] = []

            c = self._conn.cursor()

            # 1. Settings
            settings_file = bdir / "settings.json"
            if settings_file.is_file():
                try:
                    data_str = settings_file.read_text(encoding="utf-8").strip()
                    if data_str:
                        c.execute("INSERT OR REPLACE INTO settings (id, data) VALUES (?, ?)", ("app", data_str))
                        result.settings_count = 1
                        files_to_delete.append(settings_file)
                except Exception as exc:
                    logger.warning("Migration failed reading settings.json: %s", exc)

            # 2. Providers
            providers_file = bdir / "providers.json"
            if providers_file.is_file():
                try:
                    pdata = json.loads(providers_file.read_text(encoding="utf-8"))
                    if isinstance(pdata, dict):
                        for pk, pv in pdata.items():
                            name = pv.get("name") or pk
                            c.execute(
                                "INSERT OR REPLACE INTO providers (key, name, data) VALUES (?, ?, ?)",
                                (pk, name, json.dumps(pv)),
                            )
                            result.providers_count += 1
                        files_to_delete.append(providers_file)
                except Exception as exc:
                    logger.warning("Migration failed reading providers.json: %s", exc)

            # 3. Profiles
            profiles_file = bdir / "profiles.json"
            if profiles_file.is_file():
                try:
                    prof_data = json.loads(profiles_file.read_text(encoding="utf-8"))
                    if isinstance(prof_data, list):
                        for idx, p in enumerate(prof_data):
                            pid = p.get("id") or f"profile_{idx}"
                            pname = p.get("name") or pid
                            c.execute(
                                "INSERT OR REPLACE INTO profiles (id, name, data) VALUES (?, ?, ?)",
                                (pid, pname, json.dumps(p)),
                            )
                            result.profiles_count += 1
                        files_to_delete.append(profiles_file)
                except Exception as exc:
                    logger.warning("Migration failed reading profiles.json: %s", exc)

            # 4. Sessions
            sessions_index_file = bdir / "sessions.json"
            sessions_dir = bdir / "sessions"
            sess_map: dict[str, dict] = {}

            if sessions_index_file.is_file():
                try:
                    s_idx = json.loads(sessions_index_file.read_text(encoding="utf-8"))
                    if isinstance(s_idx, list):
                        for s in s_idx:
                            if isinstance(s, dict) and "id" in s:
                                sess_map[s["id"]] = s
                    files_to_delete.append(sessions_index_file)
                except Exception as exc:
                    logger.warning("Migration failed reading sessions.json: %s", exc)

            if sessions_dir.is_dir():
                dirs_to_delete.append(sessions_dir)
                for s_file in sessions_dir.glob("*.json"):
                    try:
                        content = json.loads(s_file.read_text(encoding="utf-8"))
                        s_obj = content.get("session") or sess_map.get(s_file.stem) or {"id": s_file.stem}
                        sid = s_obj.get("id", s_file.stem)
                        snum = s_obj.get("num", 0)
                        stitle = s_obj.get("title", "")
                        ui_m = content.get("ui_messages", [])
                        mem_m = content.get("mem_messages", [])
                        c.execute(
                            "INSERT OR REPLACE INTO sessions (id, num, title, data, ui_messages, mem_messages, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                sid,
                                snum,
                                stitle,
                                json.dumps(s_obj),
                                json.dumps(ui_m),
                                json.dumps(mem_m),
                                time.time(),
                            ),
                        )
                        result.sessions_count += 1
                        files_to_delete.append(s_file)
                    except Exception as exc:
                        logger.warning("Migration failed reading session file %s: %s", s_file, exc)

            # Insert any indexed sessions that did not have detail files
            for sid, s_obj in sess_map.items():
                row = c.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
                if not row:
                    c.execute(
                        "INSERT OR REPLACE INTO sessions (id, num, title, data, ui_messages, mem_messages, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            sid,
                            s_obj.get("num", 0),
                            s_obj.get("title", ""),
                            json.dumps(s_obj),
                            json.dumps([]),
                            json.dumps([]),
                            time.time(),
                        ),
                    )
                    result.sessions_count += 1

            # 5. Snapshots
            snapshots_dir = bdir / "snapshots"
            if snapshots_dir.is_dir():
                dirs_to_delete.append(snapshots_dir)
                for cfg_file in snapshots_dir.rglob("*.cfg"):
                    try:
                        cfg_txt = cfg_file.read_text(encoding="utf-8", errors="replace")
                        host = cfg_file.parent.name if cfg_file.parent != snapshots_dir else "device"
                        sid = cfg_file.stem
                        meta_obj = {"id": sid, "hostname": host, "filename": cfg_file.name}
                        c.execute(
                            "INSERT OR REPLACE INTO snapshots (id, hostname, timestamp_str, config_text, data) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (sid, host, str(cfg_file.stat().st_mtime), cfg_txt, json.dumps(meta_obj)),
                        )
                        result.snapshots_count += 1
                        files_to_delete.append(cfg_file)
                    except Exception as exc:
                        logger.warning("Migration failed reading snapshot %s: %s", cfg_file, exc)

            # 6. Transcripts
            transcripts_dir = bdir / "transcripts"
            if transcripts_dir.is_dir():
                dirs_to_delete.append(transcripts_dir)
                known_session_ids = [r[0] for r in c.execute("SELECT id FROM sessions").fetchall()]
                for t_file in transcripts_dir.glob("*.md"):
                    try:
                        t_txt = t_file.read_text(encoding="utf-8", errors="replace")
                        sess_id = t_file.stem
                        safe_title = t_file.name
                        for k_id in known_session_ids:
                            if t_file.stem.endswith(k_id):
                                sess_id = k_id
                                safe_title = t_file.stem[:-len(k_id)].rstrip("_")
                                break
                        else:
                            if "_" in t_file.stem:
                                parts = t_file.stem.rsplit("_", 1)
                                if parts[1]:
                                    sess_id = parts[1]
                                    safe_title = parts[0]

                        c.execute(
                            "INSERT INTO transcripts (session_id, safe_title, timestamp, entry) "
                            "VALUES (?, ?, ?, ?)",
                            (sess_id, safe_title, t_file.stat().st_mtime, t_txt),
                        )
                        result.transcripts_count += 1
                        files_to_delete.append(t_file)
                    except Exception as exc:
                        logger.warning("Migration failed reading transcript %s: %s", t_file, exc)

            # 7. Images
            images_dir = bdir / "images"
            if images_dir.is_dir():
                dirs_to_delete.append(images_dir)
                for img_file in images_dir.glob("*"):
                    if img_file.is_file():
                        try:
                            img_b = img_file.read_bytes()
                            c.execute(
                                "INSERT OR REPLACE INTO images (filename, data, created_at) VALUES (?, ?, ?)",
                                (img_file.name, img_b, img_file.stat().st_mtime),
                            )
                            result.images_count += 1
                            files_to_delete.append(img_file)
                        except Exception as exc:
                            logger.warning("Migration failed reading image %s: %s", img_file, exc)

            # 8. Usage History
            usage_file = bdir / "usage_history.json"
            if usage_file.is_file():
                try:
                    u_data = json.loads(usage_file.read_text(encoding="utf-8"))
                    if isinstance(u_data, list):
                        entries = u_data
                        rate_limits_obj = None
                        quotas_obj = None
                    elif isinstance(u_data, dict):
                        entries = u_data.get("entries", [])
                        rate_limits_obj = u_data.get("rate_limits")
                        quotas_obj = u_data.get("api_quotas")
                    else:
                        entries = []
                        rate_limits_obj = None
                        quotas_obj = None

                    for e in entries:
                        if not isinstance(e, dict):
                            continue
                        c.execute(
                            "INSERT INTO usage (timestamp, provider_key, model, tokens_in, tokens_out, session_id) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                e.get("ts", e.get("timestamp", time.time())),
                                e.get("prov", e.get("provider_key", "")),
                                e.get("model", ""),
                                e.get("in", e.get("tokens_in", 0)),
                                e.get("out", e.get("tokens_out", 0)),
                                e.get("sess", e.get("session_id", "")),
                            ),
                        )
                        result.usage_entries_count += 1
                    if rate_limits_obj is not None:
                        c.execute("INSERT OR REPLACE INTO usage_meta (key, data) VALUES (?, ?)", ("rate_limits", json.dumps(rate_limits_obj)))
                    if quotas_obj is not None:
                        c.execute("INSERT OR REPLACE INTO usage_meta (key, data) VALUES (?, ?)", ("api_quotas", json.dumps(quotas_obj)))
                    files_to_delete.append(usage_file)
                except Exception as exc:
                    logger.warning("Migration failed reading usage_history.json: %s", exc)

            self._conn.commit()

            # Save the migrated database to encrypted disk
            self.save()

            # ---- VERIFICATION STEP ------------------------------------------
            # Read back vault file, decrypt, and verify integrity & row counts
            try:
                test_file_bytes = self.path.read_bytes()
                test_header = test_file_bytes[:HEADER_LEN]
                test_nonce = test_file_bytes[HEADER_LEN : HEADER_LEN + NONCE_LEN]
                test_cipher = test_file_bytes[HEADER_LEN + NONCE_LEN :]

                test_plaintext = nb.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    test_cipher, test_header, test_nonce, self._data_key
                )

                test_conn = sqlite3.connect(":memory:")
                test_conn.deserialize(test_plaintext)

                integrity = test_conn.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise MigrationVerificationError(f"Integrity check failed: {integrity}")

                # Check row counts match migrated
                def _count(table: str) -> int:
                    row = test_conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                    return row[0] if row else 0

                if _count("settings") != result.settings_count:
                    raise MigrationVerificationError("Settings count mismatch in verified read-back")
                if _count("providers") != result.providers_count:
                    raise MigrationVerificationError("Providers count mismatch in verified read-back")
                if _count("profiles") != result.profiles_count:
                    raise MigrationVerificationError("Profiles count mismatch in verified read-back")
                if _count("sessions") != result.sessions_count:
                    raise MigrationVerificationError("Sessions count mismatch in verified read-back")
                if _count("snapshots") != result.snapshots_count:
                    raise MigrationVerificationError("Snapshots count mismatch in verified read-back")
                if _count("transcripts") != result.transcripts_count:
                    raise MigrationVerificationError("Transcripts count mismatch in verified read-back")
                if _count("images") != result.images_count:
                    raise MigrationVerificationError("Images count mismatch in verified read-back")
                if _count("usage") != result.usage_entries_count:
                    raise MigrationVerificationError("Usage count mismatch in verified read-back")

                test_conn.close()
            except Exception as exc:
                raise MigrationVerificationError(f"Vault read-back verification failed: {exc}") from exc

            # ---- DELETION OF PLAINTEXT ORIGINALS ----------------------------
            deleted_paths: list[str] = []
            for f in files_to_delete:
                try:
                    if f.is_file():
                        f.unlink()
                        deleted_paths.append(str(f))
                except Exception as exc:
                    logger.warning("Failed deleting plaintext file %s: %s", f, exc)

            for d in dirs_to_delete:
                try:
                    if d.is_dir():
                        # Only remove directories that are now empty so unmigrated files are never lost
                        for sub in sorted(d.rglob("*"), reverse=True):
                            if sub.is_dir():
                                try:
                                    sub.rmdir()
                                except OSError:
                                    pass
                        if not any(d.iterdir()):
                            d.rmdir()
                            deleted_paths.append(str(d))
                except Exception as exc:
                    logger.warning("Failed deleting plaintext dir %s: %s", d, exc)

            result.success = True
            result.deleted_files = deleted_paths
            return result

    # ---- database CRUD helpers ----------------------------------------------

    def _require_unlocked(self) -> sqlite3.Connection:
        if not self.is_unlocked() or self._conn is None:
            raise VaultLockedError("Vault is locked. Passphrase required to access data.")
        return self._conn

    def get_settings(self) -> Optional[dict]:
        with self._sync_lock:
            conn = self._require_unlocked()
            row = conn.execute("SELECT data FROM settings WHERE id = 'app'").fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except Exception:
                    pass
            return None

    def set_settings(self, data: dict) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute("INSERT OR REPLACE INTO settings (id, data) VALUES ('app', ?)", (json.dumps(data),))
            self.save()

    def get_providers(self) -> dict[str, dict]:
        with self._sync_lock:
            conn = self._require_unlocked()
            rows = conn.execute("SELECT key, data FROM providers").fetchall()
            out = {}
            for k, d in rows:
                try:
                    out[k] = json.loads(d)
                except Exception:
                    pass
            return out

    def set_provider(self, key: str, data: dict) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            name = data.get("name") or key
            conn.execute(
                "INSERT OR REPLACE INTO providers (key, name, data) VALUES (?, ?, ?)",
                (key, name, json.dumps(data)),
            )
            self.save()

    def delete_provider(self, key: str) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute("DELETE FROM providers WHERE key = ?", (key,))
            self.save()

    def get_profiles(self) -> list[dict]:
        with self._sync_lock:
            conn = self._require_unlocked()
            rows = conn.execute("SELECT data FROM profiles").fetchall()
            out = []
            for (d,) in rows:
                try:
                    out.append(json.loads(d))
                except Exception:
                    pass
            return out

    def set_profiles(self, profiles: list[dict]) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute("DELETE FROM profiles")
            for idx, p in enumerate(profiles):
                pid = p.get("id") or f"profile_{idx}"
                name = p.get("name") or pid
                conn.execute(
                    "INSERT INTO profiles (id, name, data) VALUES (?, ?, ?)",
                    (pid, name, json.dumps(p)),
                )
            self.save()

    def get_sessions_index(self) -> list[dict]:
        with self._sync_lock:
            conn = self._require_unlocked()
            rows = conn.execute("SELECT data FROM sessions ORDER BY num ASC, updated_at ASC").fetchall()
            out = []
            for (d,) in rows:
                try:
                    out.append(json.loads(d))
                except Exception:
                    pass
            return out

    def set_sessions_index(self, sessions_list: list[dict]) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            for s in sessions_list:
                sid = s.get("id")
                if not sid:
                    continue
                num = s.get("num", 0)
                title = s.get("title", "")
                row = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
                if row:
                    conn.execute(
                        "UPDATE sessions SET num = ?, title = ?, data = ?, updated_at = ? WHERE id = ?",
                        (num, title, json.dumps(s), time.time(), sid),
                    )
                else:
                    conn.execute(
                        "INSERT INTO sessions (id, num, title, data, ui_messages, mem_messages, updated_at) "
                        "VALUES (?, ?, ?, ?, '[]', '[]', ?)",
                        (sid, num, title, json.dumps(s), time.time()),
                    )
            self.save()

    def get_session_details(self, session_id: str) -> tuple[list, list]:
        with self._sync_lock:
            conn = self._require_unlocked()
            row = conn.execute("SELECT ui_messages, mem_messages FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return [], []
            try:
                ui_m = json.loads(row[0]) if row[0] else []
            except Exception as exc:
                logger.warning("Corrupt ui_messages JSON for session %s: %s", session_id, exc)
                ui_m = []
            try:
                mem_m = json.loads(row[1]) if row[1] else []
            except Exception as exc:
                logger.warning("Corrupt mem_messages JSON for session %s: %s", session_id, exc)
                mem_m = []
            return ui_m, mem_m

    def set_session_details(self, session_id: str, sess_dict: dict, ui_msgs: list, mem_msgs: list) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            num = sess_dict.get("num", 0)
            title = sess_dict.get("title", "")
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, num, title, data, ui_messages, mem_messages, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    num,
                    title,
                    json.dumps(sess_dict),
                    json.dumps(ui_msgs),
                    json.dumps(mem_msgs),
                    time.time(),
                ),
            )
            self.save()

    def delete_session(self, session_id: str) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM transcripts WHERE session_id = ?", (session_id,))
            self.save()

    def save_snapshot(self, snapshot_id: str, hostname: str, timestamp_str: str, config_text: str, data: dict) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (id, hostname, timestamp_str, config_text, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, hostname, timestamp_str, config_text, json.dumps(data)),
            )
            self.save()

    def get_snapshots(self, hostname: Optional[str] = None) -> list[tuple[dict, str]]:
        with self._sync_lock:
            conn = self._require_unlocked()
            if hostname:
                rows = conn.execute(
                    "SELECT data, config_text FROM snapshots WHERE hostname = ? ORDER BY timestamp_str DESC",
                    (hostname,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data, config_text FROM snapshots ORDER BY timestamp_str DESC"
                ).fetchall()
            out = []
            for d, cfg in rows:
                try:
                    out.append((json.loads(d), cfg))
                except Exception:
                    pass
            return out

    def append_transcript(self, session_id: str, safe_title: str, entry: str) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute(
                "INSERT INTO transcripts (session_id, safe_title, timestamp, entry) VALUES (?, ?, ?, ?)",
                (session_id, safe_title, time.time(), entry),
            )
            self.save()

    def get_transcripts(self, session_id: str) -> list[str]:
        with self._sync_lock:
            conn = self._require_unlocked()
            escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT entry FROM transcripts WHERE session_id = ? OR session_id LIKE ? ESCAPE '\\' ORDER BY id ASC",
                (session_id, f"%_{escaped}"),
            ).fetchall()
            return [r[0] for r in rows]

    def save_image(self, filename: str, data: bytes) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute(
                "INSERT OR REPLACE INTO images (filename, data, created_at) VALUES (?, ?, ?)",
                (filename, data, time.time()),
            )
            self.save()

    def get_image(self, filename: str) -> Optional[bytes]:
        with self._sync_lock:
            conn = self._require_unlocked()
            row = conn.execute("SELECT data FROM images WHERE filename = ?", (filename,)).fetchone()
            return row[0] if row else None

    def save_usage(self, entries: list[dict], rate_limits: dict, quotas: dict) -> None:
        with self._sync_lock:
            conn = self._require_unlocked()
            conn.execute("DELETE FROM usage")
            for e in entries:
                conn.execute(
                    "INSERT INTO usage (timestamp, provider_key, model, tokens_in, tokens_out, session_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        e.get("ts", e.get("timestamp", time.time())),
                        e.get("prov", e.get("provider_key", "")),
                        e.get("model", ""),
                        e.get("in", e.get("tokens_in", 0)),
                        e.get("out", e.get("tokens_out", 0)),
                        e.get("sess", e.get("session_id", "")),
                    ),
                )
            conn.execute("INSERT OR REPLACE INTO usage_meta (key, data) VALUES ('rate_limits', ?)", (json.dumps(rate_limits),))
            conn.execute("INSERT OR REPLACE INTO usage_meta (key, data) VALUES ('api_quotas', ?)", (json.dumps(quotas),))
            self.save()

    def load_usage(self) -> tuple[list[dict], dict, dict]:
        with self._sync_lock:
            conn = self._require_unlocked()
            rows = conn.execute(
                "SELECT timestamp, provider_key, model, tokens_in, tokens_out, session_id FROM usage ORDER BY id ASC"
            ).fetchall()
            entries = [
                {
                    "timestamp": r[0],
                    "provider_key": r[1],
                    "model": r[2],
                    "tokens_in": r[3],
                    "tokens_out": r[4],
                    "session_id": r[5],
                }
                for r in rows
            ]
            rl_row = conn.execute("SELECT data FROM usage_meta WHERE key = 'rate_limits'").fetchone()
            q_row = conn.execute("SELECT data FROM usage_meta WHERE key = 'api_quotas'").fetchone()
            try:
                rate_limits = json.loads(rl_row[0]) if rl_row and rl_row[0] else {}
            except Exception as exc:
                logger.warning("Corrupt rate_limits JSON in vault: %s", exc)
                rate_limits = {}
            try:
                quotas = json.loads(q_row[0]) if q_row and q_row[0] else {}
            except Exception as exc:
                logger.warning("Corrupt api_quotas JSON in vault: %s", exc)
                quotas = {}
            return entries, rate_limits, quotas


def get_vault(path: Optional[Path] = None) -> Vault:
    return Vault.get_instance(path)
