"""
Phase 33 verification: Encrypted Memory Backup & Disaster Recovery.

Standalone script (no pytest dependency assumed) -- run from the repo root:
    python3 tests/test_phase33_backup.py

Exercises:
  - config.py Phase 33 settings (backup_enabled, backup_dir,
    backup_retention_count, backup_passphrase, backup_cron)
  - src/system/backup.py: _derive_key (deterministic + salt-sensitive),
    create_backup/decrypt_backup round trip, _enforce_retention,
    list_backups
  - scripts/restore_backup.py: restore() round trip incl. memory_pre_restore
    safety-net copy
  - src/main.py wiring: backup_enabled gate, create_backup import,
    "encrypted_memory_backup" cron job id

Also re-runs Phase 21/22/23/28/29/34 regression suites to confirm no
breakage from the Phase 33 changes (config.py, main.py).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# chromadb is a heavy optional dependency not installed in this sandbox.
# src.system.backup doesn't import it at module level, but stub it
# defensively for parity with the other Phase tests / in case create_backup
# tries to reach get_chroma().
if "chromadb" not in sys.modules:
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.core.config import get_settings  # noqa: E402
from src.system.backup import (  # noqa: E402
    _derive_key,
    _enforce_retention,
    create_backup,
    decrypt_backup,
    list_backups,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


settings = get_settings()


# =============================================================================
print("=== Config: Phase 33 settings ===")
# =============================================================================
check("backup_enabled exists", hasattr(settings, "backup_enabled"))
check("backup_enabled defaults False", settings.backup_enabled is False)
check("backup_dir exists", hasattr(settings, "backup_dir"))
check("backup_dir defaults 'backups/'", settings.backup_dir == "backups/")
check("backup_retention_count exists", hasattr(settings, "backup_retention_count"))
check("backup_retention_count defaults 7", settings.backup_retention_count == 7)
check("backup_passphrase exists", hasattr(settings, "backup_passphrase"))
check("backup_passphrase defaults None", settings.backup_passphrase is None)
check("backup_cron exists", hasattr(settings, "backup_cron"))
check("backup_cron defaults '0 3 * * *'", settings.backup_cron == "0 3 * * *")


# =============================================================================
print("\n=== _derive_key ===")
# =============================================================================
salt_a = os_urandom_16 = __import__("os").urandom(16)
salt_b = __import__("os").urandom(16)

key1 = _derive_key("hunter2", salt_a, iterations=1000)
key2 = _derive_key("hunter2", salt_a, iterations=1000)
key3 = _derive_key("hunter2", salt_b, iterations=1000)
key4 = _derive_key("different", salt_a, iterations=1000)

check("same passphrase + salt -> identical key", key1 == key2)
check("same passphrase, different salt -> different key", key1 != key3)
check("different passphrase, same salt -> different key", key1 != key4)


# =============================================================================
print("\n=== create_backup / decrypt_backup round trip ===")
# =============================================================================
with tempfile.TemporaryDirectory() as tmpdir:
    tmp_root = Path(tmpdir)
    cwd_before = os.getcwd()
    try:
        os.chdir(tmp_root)

        # Build a dummy "memory" tree to back up.
        (tmp_root / "memory" / "chroma").mkdir(parents=True)
        (tmp_root / "memory" / "chroma" / "dummy.bin").write_bytes(b"chroma-vector-data")
        (tmp_root / "memory" / "cache").mkdir(parents=True)
        (tmp_root / "memory" / "cache" / "sessions.json").write_text('{"sessions": []}')

        backup_path = asyncio.run(create_backup(
            backup_dir="backups_test",
            passphrase="test-passphrase-123",
            retention_count=7,
            source_paths=["memory/chroma", "memory/cache/sessions.json"],
            iterations=1000,
        ))

        check("create_backup returns a Path", isinstance(backup_path, Path))
        check("backup file exists", backup_path.exists())
        check("backup filename matches tronx_backup_*.tar.enc pattern",
              backup_path.name.startswith("tronx_backup_") and backup_path.name.endswith(".tar.enc"),
              backup_path.name)

        # Wrong passphrase should fail to decrypt.
        wrong_pw_failed = False
        try:
            decrypt_backup(backup_path, "wrong-passphrase")
        except Exception:
            wrong_pw_failed = True
        check("decrypt_backup fails with wrong passphrase", wrong_pw_failed)

        raw_tar = decrypt_backup(backup_path, "test-passphrase-123")
        check("decrypt_backup returns bytes", isinstance(raw_tar, bytes) and len(raw_tar) > 0)

        import io
        with tarfile.open(fileobj=io.BytesIO(raw_tar)) as tf:
            names = set(tf.getnames())
            check("tar contains memory/chroma/dummy.bin", "memory/chroma/dummy.bin" in names, names)
            check("tar contains memory/cache/sessions.json", "memory/cache/sessions.json" in names, names)
            extracted = tf.extractfile("memory/cache/sessions.json").read()
            check("extracted sessions.json content matches", extracted == b'{"sessions": []}', extracted)

        # Missing passphrase raises ValueError.
        missing_pw_raised = False
        try:
            asyncio.run(create_backup(
                backup_dir="backups_test2",
                passphrase=None,
                source_paths=["memory/chroma"],
                iterations=1000,
            ))
        except ValueError:
            missing_pw_raised = True
        check("create_backup raises ValueError with no passphrase configured", missing_pw_raised)

        # ---------------------------------------------------------------
        print("\n=== _enforce_retention / list_backups ===")
        # ---------------------------------------------------------------
        retention_dir = tmp_root / "backups_retention"
        retention_dir.mkdir()
        for i in range(7):
            (retention_dir / f"tronx_backup_2026010{i+1}T000000Z.tar.enc").write_bytes(b"x")

        all_backups = list_backups(backup_dir=str(retention_dir))
        check("list_backups finds all 7 dummy archives", len(all_backups) == 7, len(all_backups))
        check("list_backups returns oldest-first", all_backups[0].name.endswith("20260101T000000Z.tar.enc"), all_backups[0].name)

        removed = _enforce_retention(retention_dir, 3)
        check("_enforce_retention removes 4 oldest of 7 (retention=3)", len(removed) == 4, len(removed))

        remaining = list_backups(backup_dir=str(retention_dir))
        check("3 backups remain after retention enforcement", len(remaining) == 3, len(remaining))
        check("remaining backups are the 3 newest",
              {p.name for p in remaining} == {
                  "tronx_backup_20260105T000000Z.tar.enc",
                  "tronx_backup_20260106T000000Z.tar.enc",
                  "tronx_backup_20260107T000000Z.tar.enc",
              },
              {p.name for p in remaining})

        # ---------------------------------------------------------------
        print("\n=== scripts/restore_backup.py: restore() round trip ===")
        # ---------------------------------------------------------------
        from scripts.restore_backup import restore

        target_root = tmp_root / "target"
        (target_root / "memory").mkdir(parents=True)
        (target_root / "memory" / "old_marker.txt").write_text("old-data")

        result = restore(str(backup_path), "test-passphrase-123", yes=True, target_root=target_root)

        check("restore() reports success", result.get("success") is True, result)
        check("restore() returns extracted entry list", "memory/chroma/dummy.bin" in result.get("entries", []), result.get("entries"))
        check("restored memory/chroma/dummy.bin has correct content",
              (target_root / "memory" / "chroma" / "dummy.bin").read_bytes() == b"chroma-vector-data")
        check("restored memory/cache/sessions.json has correct content",
              (target_root / "memory" / "cache" / "sessions.json").read_text() == '{"sessions": []}')

        pre_restore_dir = target_root / "memory_pre_restore"
        check("memory_pre_restore safety copy created", pre_restore_dir.exists())
        check("memory_pre_restore preserves old marker file",
              (pre_restore_dir / "old_marker.txt").read_text() == "old-data" if (pre_restore_dir / "old_marker.txt").exists() else False)

        # restore() with wrong passphrase -> graceful failure dict
        bad_result = restore(str(backup_path), "wrong-passphrase", yes=True, target_root=target_root)
        check("restore() with wrong passphrase returns success=False", bad_result.get("success") is False, bad_result)

        # restore() with nonexistent file -> graceful failure dict
        missing_result = restore(str(tmp_root / "nope.tar.enc"), "x", yes=True, target_root=target_root)
        check("restore() with missing file returns success=False", missing_result.get("success") is False, missing_result)

    finally:
        os.chdir(cwd_before)


# =============================================================================
print("\n=== src/main.py wiring ===")
# =============================================================================
main_src = (Path(ROOT) / "src" / "main.py").read_text()
check("main.py checks settings.backup_enabled", "settings.backup_enabled" in main_src)
check("main.py checks settings.backup_passphrase", "settings.backup_passphrase" in main_src)
check("main.py imports create_backup from src.system.backup", "from src.system.backup import create_backup" in main_src)
check("main.py registers 'encrypted_memory_backup' cron job", '"encrypted_memory_backup"' in main_src)
check("main.py uses settings.backup_cron for cron_expr", "cron_expr=settings.backup_cron" in main_src)
check("main.py logs disabled state when BACKUP_ENABLED=false", "[backup] Disabled (BACKUP_ENABLED=false)" in main_src)


# =============================================================================
print("\n=== Regression: Phase 21/22/23/28/29/34 suites ===")
# =============================================================================
regression_suites = [
    "tests/test_phase21_supervisor.py",
    "tests/test_phase22_intent_cache.py",
    "tests/test_phase23_pruning.py",
    "tests/test_phase28_self_healing.py",
    "tests/test_phase29_ollama_fallback.py",
    "tests/test_phase34_cost_dashboard.py",
]
for suite in regression_suites:
    proc = subprocess.run(
        [sys.executable, suite],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    check(f"{suite} passes (exit 0)", proc.returncode == 0,
          proc.stdout[-500:] + proc.stderr[-500:] if proc.returncode != 0 else "")


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
