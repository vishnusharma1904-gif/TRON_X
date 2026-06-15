"""
TRON-X Rate Limiter — per-IP + per-key sliding window

Configuration (.env):
    RATE_LIMIT_ENABLED=true
    RATE_LIMIT_RPM=60          # requests per minute per identity
    RATE_LIMIT_BURST=10        # extra burst allowance on top of RPM/60 per second
    RATE_LIMIT_SKIP_PATHS=/health,/static

How it works
------------
Uses a sliding-window log per identity (IP or API key).
Timestamps older than the window (60 s) are evicted on each check.
Thread-safe via asyncio — no locking needed (single-threaded event loop).

Returns 429 with:
  Retry-After: <seconds>
  X-RateLimit-Limit: <rpm>
  X-RateLimit-Remaining: <n>
  X-RateLimit-Reset: <epoch>
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.core.logger import log


# ---------------------------------------------------------------------------
# In-memory sliding-window store
# ---------------------------------------------------------------------------

_windows: dict[str, Deque[float]] = {}   # identity -> timestamps
_WINDOW  = 60.0                           # seconds


def _identity(request: Request) -> str:
    """Key to rate-limit by: API key if present, else client IP."""
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if key:
        # Use first 8 chars as a non-sensitive identity label
        return "key:" + key[:8]
    # Try X-Forwarded-For first (behind proxy / Docker)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return "ip:" + forwarded.split(",")[0].strip()
    client = request.client
    return "ip:" + (client.host if client else "unknown")


def _check(identity: str, limit: int) -> tuple[bool, int, float]:
    """
    Slide the window and check against limit.
    Returns (allowed, remaining, reset_epoch).
    """
    now = time.monotonic()
    epoch_now = time.time()
    window_start = now - _WINDOW

    if identity not in _windows:
        _windows[identity] = deque()

    dq = _windows[identity]

    # Evict stale entries
    while dq and dq[0] < window_start:
        dq.popleft()

    count = len(dq)
    remaining = max(0, limit - count - 1)
    reset_epoch = epoch_now + (_WINDOW - (now - dq[0]) if dq else _WINDOW)

    if count >= limit:
        return False, 0, reset_epoch

    dq.append(now)
    return True, remaining, reset_epoch


def _skip_path(path: str, skip_list: list[str]) -> bool:
    for prefix in skip_list:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# NOTE: Use @app.middleware("http") — NOT BaseHTTPMiddleware — so SSE /
# StreamingResponse endpoints are not buffered (which freezes the UI).

def add_rate_limit_middleware(app: FastAPI) -> None:
    """Register the rate-limit middleware on the FastAPI app."""
    from src.core.config import settings

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next):
        if not getattr(settings, "rate_limit_enabled", False):
            return await call_next(request)

        raw_skip = getattr(settings, "rate_limit_skip_paths", "/health,/static") or ""
        skip_list = [p.strip() for p in raw_skip.split(",") if p.strip()]

        if _skip_path(request.url.path, skip_list):
            return await call_next(request)

        limit = int(getattr(settings, "rate_limit_rpm", 60) or 60)
        identity = _identity(request)
        allowed, remaining, reset_epoch = _check(identity, limit)

        headers = {
            "X-RateLimit-Limit":     str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset":     str(int(reset_epoch)),
        }

        if not allowed:
            retry_after = max(1, int(reset_epoch - time.time()))
            log.warning("[ratelimit] 429 for %s on %s", identity, request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": f"Rate limit: {limit} requests/minute. Retry after {retry_after}s.",
                },
                headers={**headers, "Retry-After": str(retry_after)},
            )

        response = await call_next(request)
        from fastapi.responses import StreamingResponse
        if not isinstance(response, StreamingResponse):
            for k, v in headers.items():
                response.headers[k] = v
        return response

    log.info("[ratelimit] Rate-limit middleware registered (RATE_LIMIT_ENABLED=%s, RPM=%s)",
             getattr(settings, "rate_limit_enabled", False),
             getattr(settings, "rate_limit_rpm", 60))


# ---------------------------------------------------------------------------
# Utility: expose current stats (used by /api/analytics or /api/health)
# ---------------------------------------------------------------------------

def rate_limit_stats() -> dict:
    """Return a snapshot of active rate-limit windows."""
    now = time.monotonic()
    result = {}
    for identity, dq in list(_windows.items()):
        active = sum(1 for ts in dq if ts >= now - _WINDOW)
        if active:
            result[identity] = active
    return {"window_seconds": int(_WINDOW), "active_identities": len(result), "counts": result}
