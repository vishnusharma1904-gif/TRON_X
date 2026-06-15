"""
TRON-X Security Audit Log
-------------------------
Append-only JSONL record of every security operation: who, target, tier,
scope decision, and outcome. Tamper-evident enough for a homelab/self-training
record; not a substitute for a real SIEM.

    audit("recon", target="example.com", tier=0, allowed=True, engagement="homelab")
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from src.core.logger import log

_AUDIT_PATH = Path(__file__).resolve().parents[2] / "logs" / "security_audit.jsonl"


def audit(action: str, *, target: str = "", tier: int = 0,
          allowed: bool = False, engagement: str | None = None,
          reason: str = "", extra: dict[str, Any] | None = None) -> None:
    """Append one structured audit event. Never raises."""
    record = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "action": action,
        "target": target,
        "tier": tier,
        "allowed": allowed,
        "engagement": engagement,
        "reason": reason,
        "pid": os.getpid(),
    }
    if extra:
        record["extra"] = extra
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as ex:  # noqa: BLE001
        log.error("[security] failed to write audit record: %s", ex)


def read_audit(limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent `limit` audit events (newest last)."""
    if not _AUDIT_PATH.exists():
        return []
    lines = _AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
