"""
TRON-X Analytics API  --  Phase 17

GET  /api/analytics/summary     -- totals for last N days
GET  /api/analytics/chat        -- chat breakdown by intent / model / persona
GET  /api/analytics/agents      -- agent call counts + avg latency
GET  /api/analytics/endpoints   -- top N API endpoints by hit count
GET  /api/analytics/models      -- persistent model usage + success rate + live p50/p95
GET  /api/analytics/dashboard   -- [Phase 34] unified cost & usage summary
GET  /api/analytics/errors      -- recent error log
GET  /api/analytics/timeline    -- requests per hour (chart-ready)
DELETE /api/analytics/reset     -- wipe all data (confirm guard)
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from src.analytics.collector import get_collector

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@router.get("/summary")
async def analytics_summary(
    days: int = Query(default=7, ge=1, le=90, description="Look-back window in days"),
):
    """
    High-level totals: requests, chat calls, agent calls, unique sessions, errors.
    """
    return await get_collector().summary(days)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@router.get("/chat")
async def chat_analytics(
    days: int = Query(default=7, ge=1, le=90),
):
    """
    Chat breakdown by intent (coding, research, chat, ...) and by model.
    Includes success rates and average latency per group.
    """
    return await get_collector().chat_stats(days)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@router.get("/agents")
async def agent_analytics(
    days: int = Query(default=7, ge=1, le=90),
):
    """
    Per-agent call counts, average latency, and success rate.
    Covers all agents called through the TaskCoordinator.
    """
    return await get_collector().agent_stats(days)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/endpoints")
async def endpoint_analytics(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Top API endpoints by call count with average latency and success rate.
    Path parameters are normalised (e.g. /api/chat/abc123 -> /api/chat/{id}).
    """
    return await get_collector().endpoint_stats(limit, days)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@router.get("/models")
async def model_analytics(
    days: int = Query(default=7, ge=1, le=90),
):
    """
    Persistent model usage stats (survives server restarts, unlike the
    in-memory LatencyTracker). Includes live p50/p95 from the router when
    available.
    """
    return await get_collector().model_stats(days)


# ---------------------------------------------------------------------------
# Cost & Usage Dashboard  [Phase 34]
# ---------------------------------------------------------------------------

@router.get("/dashboard")
async def cost_usage_dashboard(
    period: str = Query(default="7d", description="Look-back window, e.g. '24h', '7d', '30d'"),
):
    """
    Unified Cost & Usage Dashboard.

    Aggregates chat_events into total/by-provider/by-model/by-intent cost
    estimates (from config/pricing.json), token totals, and a list of any
    models with no pricing entry (`unpriced_models`). `pricing_last_updated`
    flags how stale the rate table might be.

    `cache_hit_rate` is reserved for Phase 22 (Local Intent Cache) and is
    `None` until that phase lands. `circuit_breaker_events` is populated
    from Phase 28's diagnostics log when available, else [].
    """
    return await get_collector().get_cost_summary(period)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@router.get("/errors")
async def error_analytics(
    limit: int = Query(default=50, ge=1, le=500),
):
    """Most recent error events (5xx responses + explicitly recorded errors)."""
    return await get_collector().error_log(limit)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

@router.get("/timeline")
async def timeline_analytics(
    hours: int = Query(default=24, ge=1, le=168, description="Look-back window in hours (max 168 = 7 days)"),
):
    """
    Requests per hour for the last N hours.
    Returns chart-ready data: [{hour: '2026-06-08T14', count: 42, errors: 3}]
    """
    return await get_collector().timeline(hours)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class ResetRequest(BaseModel):
    confirm: bool = False


@router.delete("/reset")
async def reset_analytics(req: ResetRequest):
    """
    Wipe all analytics data permanently.
    Must pass confirm=true in the request body.
    """
    return await get_collector().reset(req.confirm)
