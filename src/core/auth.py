"""
TRON-X API Key Authentication

Provides two integration points:
  1. add_auth_middleware(app)  -- HTTP middleware (blanket, low overhead)
  2. require_api_key           -- FastAPI Depends() for per-route protection

Configuration (.env):
    AUTH_ENABLED=true
    API_KEYS=key1,key2,key3          # comma-separated list of valid keys
    AUTH_SKIP_PATHS=/health,/docs,/redoc,/static,/openapi.json

Usage in main.py:
    from src.core.auth import add_auth_middleware
    add_auth_middleware(app)         # called after app creation
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, APIKeyQuery
from starlette.responses import JSONResponse

from src.core.logger import log

# FastAPI security schemes (for /docs Authorize button)
_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_query_scheme  = APIKeyQuery(name="api_key",     auto_error=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_settings():
    from src.core.config import settings
    return settings


def _load_keys() -> frozenset[str]:
    """Return the set of valid API keys from settings."""
    s = _get_settings()
    raw: str = getattr(s, "api_keys", "") or ""
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    return frozenset(keys)


def _skip_path(path: str) -> bool:
    """Return True if this path should bypass auth checks."""
    s = _get_settings()
    raw: str = getattr(s, "auth_skip_paths",
                       "/health,/docs,/redoc,/static,/openapi.json") or ""
    skip = [p.strip() for p in raw.split(",") if p.strip()]
    for prefix in skip:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return True
    return False


def _extract_key(request: Request) -> Optional[str]:
    """Pull API key from X-API-Key header or ?api_key= query param."""
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    return key or None


def _validate(key: Optional[str], valid_keys: frozenset[str]) -> bool:
    if not key:
        return False
    # Constant-time comparison against each valid key
    return any(secrets.compare_digest(key, vk) for vk in valid_keys)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# NOTE: Use @app.middleware("http") — NOT BaseHTTPMiddleware — so SSE /
# StreamingResponse endpoints (chat, voice, proactive) are not buffered.

def add_auth_middleware(app: FastAPI) -> None:
    """Register the auth middleware on the FastAPI app."""

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        s = _get_settings()
        if not getattr(s, "auth_enabled", False):
            return await call_next(request)

        if _skip_path(request.url.path):
            return await call_next(request)

        valid_keys = _load_keys()
        if not valid_keys:
            log.warning("[auth] AUTH_ENABLED=true but API_KEYS is empty — all requests allowed")
            return await call_next(request)

        key = _extract_key(request)
        if not _validate(key, valid_keys):
            log.warning("[auth] Rejected %s %s (bad/missing key)", request.method, request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorised", "detail": "Valid X-API-Key required"},
                headers={"WWW-Authenticate": "ApiKey"},
            )

        return await call_next(request)

    log.info("[auth] Auth middleware registered (AUTH_ENABLED=%s)",
             getattr(_get_settings(), "auth_enabled", False))


# ---------------------------------------------------------------------------
# Per-route dependency (alternative to blanket middleware)
# ---------------------------------------------------------------------------

async def require_api_key(
    header_key: Optional[str] = Security(_header_scheme),
    query_key:  Optional[str] = Security(_query_scheme),
) -> str:
    """
    FastAPI dependency for protecting individual routes.
    Usage:  @router.get("/secret", dependencies=[Depends(require_api_key)])
    """
    s = _get_settings()
    if not getattr(s, "auth_enabled", False):
        return "dev"   # auth off — pass through

    valid_keys = _load_keys()
    key = header_key or query_key
    if valid_keys and _validate(key, valid_keys):
        return key

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )
