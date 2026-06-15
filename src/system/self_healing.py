"""
TRON-X Self-Healing
────────────────────
Phase 28: Proactive Cron Analytics & Diagnostic Self-Healing.

A periodic background job (registered via SchedulerAgent.add_interval_job in
src/main.py's lifespan, gated by SELF_HEALING_ENABLED) that:

  1. Reads CPU / RAM / disk via psutil (mirrors src.system.control.get_system_info).
  2. Reads SmartRouter.health.get_status_summary() (tripped models, trip counts).
  3. Takes conditional remediation actions:
       - RAM  >= RAM_THRESHOLD_PCT   -> gc.collect() + drop bounded in-process
                                         caches (router latency tracker window).
       - DISK >= DISK_THRESHOLD_PCT  -> trim oversized log files + purge
                                         conversation turns >30 days old.
                                         NEVER touches the 'knowledge' collection
                                         (remembered facts) -- only the rolling
                                         'conversations' history.
       - >= CIRCUIT_TRIP_REORDER_THRESHOLD models tripped
                                      -> SmartRouter.bias_fallback_chain() toward
                                         a healthy model (auto-reverts via TTL or
                                         once the tripped models recover).
  4. Logs every run (resources + router health + actions taken) to
     memory/cache/diagnostics.jsonl as one JSON line per run (ring-buffer
     trimmed to the most recent _MAX_DIAG_LINES entries).

Anti-thrashing: if a threshold condition persists across consecutive runs
without improvement, subsequent runs log "condition persists after <action>"
instead of repeating an action that evidently had no effect.
"""
from __future__ import annotations

import gc
import json
import time
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

_DIAG_PATH      = Path("memory/cache/diagnostics.jsonl")
_MAX_DIAG_LINES = 2000  # ring-buffer cap

# In-process state tracked across runs (single-process app -- module global is fine)
_state: dict[str, Any] = {
    "last_ram_action_pct":  None,   # RAM%% at the time we last took a RAM action
    "last_disk_action_pct": None,   # Disk%% at the time we last took a disk action
    "last_bias_model":      None,   # model we last biased toward (avoid re-trigger spam)
}


# ---------------------------------------------------------------------------
# Resource stats
# ---------------------------------------------------------------------------

async def _get_resource_stats() -> dict:
    """CPU / RAM / disk usage. Mirrors src.system.control.get_system_info()."""
    try:
        import psutil
        return {
            "cpu_percent":   psutil.cpu_percent(interval=0.5),
            "ram_used_pct":  psutil.virtual_memory().percent,
            "disk_used_pct": psutil.disk_usage("/").percent,
        }
    except ImportError:
        return {"error": "pip install psutil"}


# ---------------------------------------------------------------------------
# Diagnostics log (memory/cache/diagnostics.jsonl)
# ---------------------------------------------------------------------------

def _log_diagnostic(entry: dict) -> None:
    """Append one JSON line; ring-buffer-trim the file to _MAX_DIAG_LINES."""
    try:
        _DIAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DIAG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        lines = _DIAG_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_DIAG_LINES:
            _DIAG_PATH.write_text("\n".join(lines[-_MAX_DIAG_LINES:]) + "\n", encoding="utf-8")
    except Exception as e:
        log.debug(f"[self_healing] diagnostics log write failed: {e}")


async def get_diagnostics_log(limit: int = 50) -> dict:
    """Read the most recent `limit` diagnostics entries (oldest -> newest)."""
    if not _DIAG_PATH.exists():
        return {"entries": [], "count": 0}
    try:
        lines = _DIAG_PATH.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return {"entries": [], "count": 0, "error": str(e)}

    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return {"entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# Remediation actions
# ---------------------------------------------------------------------------

def _clear_memory_caches() -> list[str]:
    """Best-effort in-process cache clears + GC. Returns actions taken."""
    actions = []
    collected = gc.collect()
    actions.append(f"gc.collect() freed {collected} object(s)")

    # Bounded rolling windows are cheap to drop and rebuild from live traffic.
    try:
        from src.intelligence.router import get_router
        router = get_router()
        tracker = getattr(router, "latency_tracker", None)
        if tracker is not None:
            data = getattr(tracker, "_data", None)
            if data is not None:
                n_models = len(data)
                data.clear()
                if n_models:
                    actions.append(f"cleared latency_tracker history ({n_models} model(s))")
    except Exception as e:
        log.debug(f"[self_healing] router cache clear skipped: {e}")

    return actions


async def _purge_old_data() -> list[str]:
    """
    Best-effort disk cleanup under pressure:
      1. Trim oversized log files in logs/ (keep last 2MB of any file >5MB).
      2. Purge conversation turns older than 30 days from ChromaDB.
         NEVER touches COL_KNOWLEDGE (remembered facts), COL_DOCUMENTS, or
         COL_EPISODES -- only the rolling COL_CONVERSATIONS history.
    """
    actions: list[str] = []

    try:
        logs_dir = Path("logs")
        if logs_dir.exists():
            for log_file in logs_dir.glob("*.log"):
                try:
                    size = log_file.stat().st_size
                    if size > 5 * 1024 * 1024:  # 5MB
                        data = log_file.read_bytes()[-2 * 1024 * 1024:]  # keep last 2MB
                        log_file.write_bytes(data)
                        actions.append(f"trimmed {log_file.name} ({size} -> {len(data)} bytes)")
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"[self_healing] log trim skipped: {e}")

    try:
        from src.memory.chroma_db import get_chroma
        chroma = get_chroma()
        removed = await chroma.delete_old_conversations(days=30)
        if removed:
            actions.append(f"purged {removed} conversation turn(s) older than 30d")
    except Exception as e:
        log.debug(f"[self_healing] conversation purge skipped: {e}")

    return actions


def _pick_healthy_model(router, tripped: list[str]) -> Optional[str]:
    """Find the first available, non-tripped model anywhere in the catalog
    to bias fallback chains toward."""
    tripped_set = set(tripped)
    try:
        for cat_data in router.catalog["categories"].values():
            chain = [cat_data["primary"]] + cat_data.get("fallbacks", [])
            for model_id in chain:
                if model_id in tripped_set:
                    continue
                if router.health.is_available(model_id):
                    return model_id
    except Exception as e:
        log.debug(f"[self_healing] _pick_healthy_model failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_health_check() -> dict:
    """
    Run a single self-healing cycle. Returns the diagnostics entry (also
    appended to memory/cache/diagnostics.jsonl). Safe to call repeatedly.
    """
    ts = time.time()
    resources = await _get_resource_stats()

    try:
        from src.intelligence.router import get_router
        router = get_router()
        router_health = router.health.get_status_summary()
    except Exception as e:
        router = None
        router_health = {"error": str(e), "tripped_models": [], "trip_counts": {}}

    actions: list[str] = []

    ram_pct  = resources.get("ram_used_pct")
    disk_pct = resources.get("disk_used_pct")

    # -- RAM remediation ------------------------------------------------------
    if isinstance(ram_pct, (int, float)) and ram_pct >= settings.ram_threshold_pct:
        prev = _state["last_ram_action_pct"]
        if prev is not None and ram_pct >= prev:
            actions.append(
                f"RAM still at {ram_pct:.1f}% (threshold {settings.ram_threshold_pct}%) "
                f"after previous cache clear -- condition persists"
            )
        else:
            actions.extend(_clear_memory_caches())
        _state["last_ram_action_pct"] = ram_pct
    else:
        _state["last_ram_action_pct"] = None

    # -- Disk remediation ------------------------------------------------------
    if isinstance(disk_pct, (int, float)) and disk_pct >= settings.disk_threshold_pct:
        prev = _state["last_disk_action_pct"]
        if prev is not None and disk_pct >= prev:
            actions.append(
                f"Disk still at {disk_pct:.1f}% (threshold {settings.disk_threshold_pct}%) "
                f"after previous purge -- condition persists"
            )
        else:
            actions.extend(await _purge_old_data())
        _state["last_disk_action_pct"] = disk_pct
    else:
        _state["last_disk_action_pct"] = None

    # -- Circuit-breaker reorder -------------------------------------------------
    tripped = router_health.get("tripped_models", [])
    if router is not None and len(tripped) >= settings.circuit_trip_reorder_threshold:
        healthy_pref = _pick_healthy_model(router, tripped)
        if healthy_pref:
            if healthy_pref != _state["last_bias_model"]:
                router.bias_fallback_chain(healthy_pref)
                actions.append(
                    f"biased fallback chains toward {healthy_pref} "
                    f"({len(tripped)} model(s) tripped: {', '.join(tripped)})"
                )
                _state["last_bias_model"] = healthy_pref
            else:
                actions.append(
                    f"fallback bias toward {healthy_pref} already active "
                    f"({len(tripped)} model(s) still tripped) -- condition persists"
                )
        else:
            actions.append(
                f"{len(tripped)} model(s) tripped but no healthy alternative found "
                f"({', '.join(tripped)})"
            )
    else:
        _state["last_bias_model"] = None

    entry = {
        "ts":            ts,
        "resources":     resources,
        "router_health": router_health,
        "actions":       actions,
        "thresholds": {
            "ram_pct":              settings.ram_threshold_pct,
            "disk_pct":             settings.disk_threshold_pct,
            "circuit_trip_reorder": settings.circuit_trip_reorder_threshold,
        },
    }
    _log_diagnostic(entry)

    if actions:
        log.info(f"[self_healing] {len(actions)} action(s): {'; '.join(actions)}")
    else:
        log.debug("[self_healing] health check OK, no action needed")

    return entry
