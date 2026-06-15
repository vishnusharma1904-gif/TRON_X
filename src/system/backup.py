"""
TRON-X Encrypted Memory Backup & Disaster Recovery (Phase 33)
─────────────────────────────────────────────────────────────
Creates encrypted, timestamped tar archives of TRON-X's persistent memory
(ChromaDB vector store + session history, plus optional per-user state from
later phases if present) for disaster recovery. Restoration is handled by
`scripts/restore_backup.py`.

Encryption: Fernet (`cryptography` -- AES-128-CBC + HMAC-SHA256) with a key
derived from `backup_passphrase` via PBKDF2-HMAC-SHA256 (480,000 iterations,
random 16-byte salt per archive). The salt + a small version header are
stored unencrypted alongside the ciphertext in the `.tar.enc` file so the
same passphrase can be used to derive the correct key on restore.

Snapshot strategy: files are first copied into a temporary snapshot
directory under the backup lock (held only for the fast file-copy phase),
then tarred + encrypted *outside* the lock -- this avoids a torn snapshot
of the ChromaDB store while minimising lock hold time, per the Phase 33
spec's edge-case guidance.

**Reconciliation vs. spec**: the spec proposed `backup_enabled: bool = True`
and "fail loudly at startup" if `backup_passphrase` is missing while enabled.
This codebase defaults `backup_enabled=False` (opt-in) instead, because:
  - Existing installs have no `BACKUP_PASSPHRASE` configured, so a default-on
    flag would either crash startup (a regression for every current install)
    or silently no-op (defeating "fail loudly").
  - Opt-in means nothing changes for existing installs. Once a user sets
    `BACKUP_ENABLED=true`, a missing `BACKUP_PASSPHRASE` is reported via a
    startup warning (backups stay disabled) rather than crashing the app --
    consistent with this codebase's "never crash startup, degrade to no-op"
    convention (see Phase 22's `IntentCache.enabled` and Phase 28's
    `self_healing_enabled`).

Phase 25 (voice biometrics) and Phase 31 (preference learning) are not yet
implemented in this codebase, so `memory/cache/voice_profile.npy` and
`memory/cache/preferences.json` are included in `_BACKUP_PATHS` but simply
skipped (via the existence check) until those phases land -- no code changes
will be needed here when they do.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Optional

from src.core.config import get_settings
from src.core.logger import log

# Paths included in every backup, relative to the project root, if present.
_BACKUP_PATHS = [
    "memory/chroma",
    "memory/cache/sessions.json",
    "memory/cache/voice_profile.npy",   # Phase 25 (if implemented)
    "memory/cache/preferences.json",    # Phase 31 (if implemented)
]

PRE_RESTORE_DIR = "memory_pre_restore"

_PBKDF2_ITERATIONS = 480_000


def _derive_key(passphrase: str, salt: bytes, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """Derive a Fernet-compatible key from a passphrase + salt via PBKDF2-HMAC-SHA256."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _enforce_retention(backup_dir: Path, retention_count: int) -> list[Path]:
    """Delete oldest `tronx_backup_*.tar.enc` files beyond `retention_count`. Returns removed paths."""
    if retention_count < 0:
        return []
    backups = sorted(backup_dir.glob("tronx_backup_*.tar.enc"))
    removed: list[Path] = []
    while len(backups) > retention_count:
        oldest = backups.pop(0)
        try:
            oldest.unlink()
            removed.append(oldest)
        except OSError as e:
            log.warning(f"[backup] failed to remove old backup {oldest}: {e}")
    return removed


async def create_backup(
    backup_dir: Optional[str] = None,
    passphrase: Optional[str] = None,
    retention_count: Optional[int] = None,
    source_paths: Optional[list[str]] = None,
    iterations: int = _PBKDF2_ITERATIONS,
) -> Path:
    """
    Create an encrypted, timestamped backup archive.

    Args:
        backup_dir: override `settings.backup_dir` (relative or absolute).
        passphrase: override `settings.backup_passphrase`.
        retention_count: override `settings.backup_retention_count`.
        source_paths: override `_BACKUP_PATHS` (mainly for tests).
        iterations: PBKDF2 iteration count (lower only for fast tests).

    Returns:
        Path to the new `tronx_backup_<timestamp>.tar.enc` file.

    Raises:
        ValueError: if no passphrase is configured or available.
    """
    settings = get_settings()
    backup_dir_p = Path(backup_dir or settings.backup_dir)
    backup_dir_p.mkdir(parents=True, exist_ok=True)

    pw = passphrase or settings.backup_passphrase
    if not pw:
        raise ValueError("backup_passphrase is not configured -- cannot create an encrypted backup")

    paths = source_paths if source_paths is not None else _BACKUP_PATHS
    existing = [Path(p) for p in paths if Path(p).exists()]

    snapshot_dir = backup_dir_p / f"_snapshot_{int(time.time() * 1000)}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Hold the ChromaDB write lock only for the (fast) file-copy phase.
        lock = None
        try:
            from src.memory.chroma_db import get_chroma
            lock = get_chroma()._lock
        except Exception:
            lock = None

        async def _copy() -> None:
            for src in existing:
                arcname = src.relative_to(src.anchor) if src.is_absolute() else src
                dst = snapshot_dir / arcname
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)

        if lock is not None:
            async with lock:
                await _copy()
        else:
            await _copy()

        tar_path = backup_dir_p / f"_snapshot_{int(time.time() * 1000)}.tar"
        with tarfile.open(tar_path, "w") as tf:
            for item in sorted(snapshot_dir.iterdir()):
                tf.add(item, arcname=item.name)

        raw = tar_path.read_bytes()
        tar_path.unlink(missing_ok=True)

        from cryptography.fernet import Fernet
        salt = os.urandom(16)
        key = _derive_key(pw, salt, iterations=iterations)
        token = Fernet(key).encrypt(raw)

        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_path = backup_dir_p / f"tronx_backup_{timestamp}.tar.enc"
        header = json.dumps({
            "salt": urlsafe_b64encode(salt).decode("ascii"),
            "version": 1,
            "iterations": iterations,
        }).encode("utf-8")
        with open(out_path, "wb") as f:
            f.write(len(header).to_bytes(4, "big"))
            f.write(header)
            f.write(token)
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)

    removed = _enforce_retention(
        backup_dir_p,
        retention_count if retention_count is not None else settings.backup_retention_count,
    )
    log.info(f"[backup] Created {out_path.name} ({out_path.stat().st_size} bytes); removed {len(removed)} old backup(s)")
    return out_path


def decrypt_backup(path: str | Path, passphrase: str) -> bytes:
    """Decrypt a `.tar.enc` backup file, returning the raw tar bytes."""
    from cryptography.fernet import Fernet

    raw = Path(path).read_bytes()
    header_len = int.from_bytes(raw[:4], "big")
    header = json.loads(raw[4:4 + header_len])
    token = raw[4 + header_len:]

    salt = urlsafe_b64decode(header["salt"])
    key = _derive_key(passphrase, salt, iterations=header.get("iterations", _PBKDF2_ITERATIONS))
    return Fernet(key).decrypt(token)


def list_backups(backup_dir: Optional[str] = None) -> list[Path]:
    """Return all `tronx_backup_*.tar.enc` files in `backup_dir`, oldest first."""
    settings = get_settings()
    d = Path(backup_dir or settings.backup_dir)
    if not d.exists():
        return []
    return sorted(d.glob("tronx_backup_*.tar.enc"))
