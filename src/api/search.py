"""
TRON-X Search API  (Phase 3)
─────────────────────────────
Endpoints:
  POST /api/search          — one-shot search, returns JSON with synthesis + citations
  GET  /api/search/stream   — SSE stream with real-time search progress events
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.logger import log
from src.intelligence.emotion import detect_emotion
from src.intelligence.language_profile import build_language_profile
from src.intelligence.router import get_router
from src.intelligence.telugu import detect_telugu
from src.intelligence.web_search import get_web_search

router = APIRouter(prefix="/api/search", tags=["search"])


# ── Request / Response models ─────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query:   str
    intent:  str  = "research"
    persona: str  = "jarvis"


class CitationOut(BaseModel):
    index:   int
    title:   str
    url:     str
    snippet: str
    date:    str = ""


class SearchResponse(BaseModel):
    synthesis:     str
    citations:     list[CitationOut]
    queries_used:  list[str]
    provider:      str
    model_used:    str
    latency_ms:    int
    search_used:   bool = True
    search:        Optional[dict] = None
    language_profile: Optional[dict] = None
    academic_mode: bool = False


# ── POST /api/search ──────────────────────────────────────────────────────────

@router.post("", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """
    One-shot intelligent search.
    Runs full pipeline: expand → search → rank → fetch pages → synthesise.
    """
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    emotion_state = detect_emotion(req.query)
    telugu_state  = detect_telugu(req.query)
    language_profile = build_language_profile(req.query, telugu_state=telugu_state)
    router_inst   = get_router()
    ws            = get_web_search()

    try:
        result = await ws.search(
            query=req.query,
            intent=req.intent,
            persona=req.persona,
            emotion_state=emotion_state,
            telugu_state=telugu_state,
            router=router_inst,
        )
    except Exception as e:
        log.error(f"[search_api] search failed: {e}")
        raise HTTPException(status_code=502, detail=f"Search pipeline failed: {e}")

    return SearchResponse(
        synthesis=result.synthesis,
        citations=[
            CitationOut(
                index=c.index, title=c.title, url=c.url,
                snippet=c.snippet, date=c.date,
            )
            for c in result.citations
        ],
        queries_used=result.queries_used,
        provider=result.provider,
        model_used=result.model_used,
        latency_ms=result.latency_ms,
        search={
            "used": True,
            "provider": result.provider,
            "queries": result.queries_used,
            "citations": [
                {"index": c.index, "title": c.title, "url": c.url, "snippet": c.snippet, "date": c.date}
                for c in result.citations
            ],
            "latency_ms": result.latency_ms,
            "model_used": result.model_used,
        },
        language_profile=language_profile,
        academic_mode=req.intent == "academic",
    )


# ── GET /api/search/stream ────────────────────────────────────────────────────

@router.get("/stream")
async def search_stream(
    query:   str,
    intent:  str = "research",
    persona: str = "jarvis",
) -> StreamingResponse:
    """
    SSE streaming search.
    Each event is a JSON line prefixed with 'data: '.

    Event shapes:
      {type: "search_progress", step, message, queries?}
      {type: "search_result",   data: {synthesis, citations, ...}}
      {type: "error",           message}
    """
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    emotion_state = detect_emotion(query)
    telugu_state  = detect_telugu(query)
    language_profile = build_language_profile(query, telugu_state=telugu_state)
    router_inst   = get_router()
    ws            = get_web_search()

    async def event_generator():
        try:
            async for ev in ws.stream(
                query=query,
                intent=intent,
                persona=persona,
                emotion_state=emotion_state,
                telugu_state=telugu_state,
                router=router_inst,
            ):
                if ev.get("type") == "search_result":
                    ev["data"]["search"] = {
                        "used": True,
                        "provider": ev["data"].get("provider"),
                        "queries": ev["data"].get("queries_used", []),
                        "citations": ev["data"].get("citations", []),
                        "latency_ms": ev["data"].get("latency_ms"),
                        "model_used": ev["data"].get("model_used"),
                    }
                    ev["data"]["language_profile"] = language_profile
                    ev["data"]["academic_mode"] = intent == "academic"
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            log.error(f"[search_api/stream] error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
