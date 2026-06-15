"""
TRON-X Encrypted Memory Backup -- Restore CLI (Phase 33)
─────────────────────────────────────────────────────────
Restores a `.tar.enc` archive created by `src/system/backup.py`.

Usage:
    python scripts/restore_backup.py <backup_file> --passphrase <PASSPHRASE> [--yes] [--target-root DIR]

Safety:
    Before extracting, any existing `memory/` directory under the target
    root is copied to `memory_pre_restore/` (overwriting any previous
    safety copy) so a botched restore can be undone manually.

    Without --yes, the archive contents are listed and the user is
    prompted for confirmation before anything is written to disk.
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.system.backup import decrypt_backup, PRE_RESTORE_DIR  # noqa: E402


def restore(
    backup_file: str,
    passphrase: str,
    yes: bool = False,
    target_root: Optional[Path] = None,
) -> dict:
    """
    Decrypt `backup_file` and extract it under `target_root` (defaults to the
    project root). If a `memory/` directory already exists under
    `target_root`, it is first copied to `<target_root>/memory_pre_restore/`
    as a safety net.

    Returns a dict: {"success": bool, "entries": [...], "pre_restore_backup": str|None}
    """
    target_root = target_root or ROOT
    backup_path = Path(backup_file)
    if not backup_path.exists():
        return {"success": False, "error": f"Backup file not found: {backup_path}"}

    try:
        raw_tar = decrypt_backup(backup_path, passphrase)
    except Exception as e:
        return {"success": False, "error": f"Failed to decrypt backup (wrong passphrase?): {e}"}

    with tarfile.open(fileobj=io.BytesIO(raw_tar)) as tf:
        names = tf.getnames()

        if not yes:
            print(f"Backup archive: {backup_path.name}")
            print(f"Contains {len(names)} entr{'y' if len(names) == 1 else 'ies'}:")
            for n in names:
                print(f"  {n}")
            print(f"\nThis will overwrite files under: {target_root}")
            reply = input("Proceed with restore? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                return {"success": False, "error": "Restore cancelled by user"}

        memory_dir = target_root / "memory"
        pre_restore = target_root / PRE_RESTORE_DIR
        pre_restore_backup: Optional[str] = None

        if memory_dir.exists():
            if pre_restore.exists():
                shutil.rmtree(pre_restore)
            shutil.copytree(memory_dir, pre_restore)
            pre_restore_backup = str(pre_restore)

        tf.extractall(path=target_root)

    return {"success": True, "entries": names, "pre_restore_backup": pre_restore_backup}


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a TRON-X encrypted memory backup")
    parser.add_argument("backup_file", help="Path to the tronx_backup_*.tar.enc file")
    parser.add_argument("--passphrase", required=True, help="Backup encryption passphrase")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--target-root", default=None, help="Restore target root (default: project root)")
    args = parser.parse_args()

    target_root = Path(args.target_root) if args.target_root else None
    result = restore(args.backup_file, args.passphrase, yes=args.yes, target_root=target_root)

    if result.get("success"):
        print(f"\nRestore complete. {len(result['entries'])} entr"
              f"{'y' if len(result['entries']) == 1 else 'ies'} extracted.")
        if result.get("pre_restore_backup"):
            print(f"Previous memory/ saved to: {result['pre_restore_backup']}")
    else:
        print(f"Restore failed: {result.get('error')}", file=sys.stderr)

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
