"""
TRON-X Analytics Middleware  --  Phase 17

FastAPI middleware that auto-instruments every HTTP request.
Adds zero perceptible latency -- analytics writes are fire-and-forget.

Usage (in main.py):
    from src.analytics.middleware import add_analytics_middleware
    add_analytics_middleware(app)
"""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

_STREAM_PREFIXES = (
    "/api/chat/stream",
    "/api/voice/stream",
    "/api/voice/tts/stream",
    "/api/agents/",
    "/api/search/stream",
    "/api/computer/",
    "/api/proactive/events",
)


def add_analytics_middleware(app: FastAPI) -> None:
    """Register the analytics middleware on the given FastAPI app."""

    @app.middleware("http")
    async def _analytics(request: Request, call_next):
        t0       = time.monotonic()
        response = await call_next(request)

        # Streaming responses: return immediately — do not touch headers/status
        # after the stream has started (would block other requests on single worker).
        if isinstance(response, StreamingResponse):
            return response

        latency = (time.monotonic() - t0) * 1000
        path = request.url.path
        if path.startswith("/api/analytics") or path.startswith("/static"):
            return response
        if any(path.startswith(p) for p in _STREAM_PREFIXES):
            return response

        try:
            from src.analytics.collector import get_collector
            asyncio.create_task(
                get_collector().record_request(
                    method=request.method,
                    endpoint=path,
                    status_code=response.status_code,
                    latency_ms=latency,
                )
            )
        except Exception:
            pass

        return response
