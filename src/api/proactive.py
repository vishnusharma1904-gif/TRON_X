"""
TRON-X Proactive API  (Phase 37)
────────────────────────────────
GET  /api/proactive/briefing            — compose (or return cached) briefing
GET  /api/proactive/events              — recent event-bus history (poll)
GET  /api/proactive/stream              — live SSE feed of the event bus
POST /api/proactive/consolidate         — run memory consolidation now
POST /api/proactive/sentinel/run        — run one sentinel sweep now
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.event_bus import get_event_bus
from src.core.logger import log

router = APIRouter(prefix="/api/proactive", tags=["proactive"])

_SSE_HEARTBEAT_SEC = 20.0


# ── Briefing ───────────────────────────────────────────────────────────────────

@router.get("/briefing")
async def briefing(
    kind: str = Query("adhoc", pattern="^(morning|evening|adhoc)$"),
    persona: str = "jarvis",
    force: bool = False,
):
    from src.proactive.anticipator import get_anticipator
    return await get_anticipator().briefing(kind=kind, persona=persona,
                                            force=force)


# ── Event history (poll fallback) ─────────────────────────────────────────────

@router.get("/events")
async def events(limit: int = Query(50, ge=1, le=200),
                 types: Optional[str] = None):
    type_set = {t.strip() for t in types.split(",")} if types else None
    evts = get_event_bus().recent(limit=limit, event_types=type_set)
    return {"events": [e.model_dump() for e in evts], "count": len(evts)}


# ── Live SSE stream ────────────────────────────────────────────────────────────

@router.get("/stream")
async def stream(types: Optional[str] = None, backfill: int = 10):
    """Server-Sent Events feed of the live event bus."""
    type_set = {t.strip() for t in types.split(",")} if types else None
    bus = get_event_bus()
    q = bus.subscribe_queue(type_set)

    async def gen():
        try:
            # backfill recent events so a reconnecting HUD isn't blank
            for evt in bus.recent(limit=backfill, event_types=type_set):
                yield f"data: {json.dumps(evt.model_dump())}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(),
                                                 timeout=_SSE_HEARTBEAT_SEC)
                    yield f"data: {json.dumps(evt.model_dump())}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe_queue(q)
            log.debug("[proactive] SSE client disconnected")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── Manual triggers ────────────────────────────────────────────────────────────

class ConsolidateReq(BaseModel):
    retention_days: Optional[int] = None
    prune: Optional[bool] = None


@router.post("/consolidate")
async def consolidate_now(req: ConsolidateReq):
    from src.proactive.consolidation import consolidate
    return await consolidate(retention_days=req.retention_days,
                             prune=req.prune)


@router.post("/sentinel/run")
async def sentinel_run():
    from src.proactive.triggers import get_sentinel
    return await get_sentinel().run_once()
