"""
TRON-X System Health API  --  Phase 28
----------------------------------------
Exposes the proactive self-healing diagnostics:
  - GET  /api/system/health/status       -- live router health + bias state
  - GET  /api/system/health/diagnostics  -- recent self-healing run history
  - POST /api/system/health/check        -- run a self-healing cycle now
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from src.core.config import get_settings
from src.intelligence.router import get_router
from src.system.self_healing import get_diagnostics_log, run_health_check

router = APIRouter(prefix="/api/system/health", tags=["system-health"])
settings = get_settings()


@router.get("/status")
async def health_status():
    """Live circuit-breaker health + self-healing bias state + thresholds."""
    r = get_router()
    return {
        "router_health":  r.health.get_status_summary(),
        "self_healing": {
            "enabled":      settings.self_healing_enabled,
            "interval_sec": settings.self_healing_interval_sec,
            "bias":         r.bias_status(),
        },
        "thresholds": {
            "ram_pct":              settings.ram_threshold_pct,
            "disk_pct":             settings.disk_threshold_pct,
            "circuit_trip_reorder": settings.circuit_trip_reorder_threshold,
        },
    }


@router.get("/diagnostics")
async def diagnostics(limit: int = Query(default=50, ge=1, le=500)):
    """Recent self-healing run history from memory/cache/diagnostics.jsonl."""
    return await get_diagnostics_log(limit)


@router.post("/check")
async def trigger_check():
    """Run one self-healing cycle immediately and return its result."""
    return await run_health_check()
