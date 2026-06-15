"""
TRON-X API Key Authentication & Per-Browser Identity

TRON-X has no login system. Instead:
  - Every browser gets an anonymous, random `user_id` (generated client-side
    by static/js/auth-gate.js and stored in localStorage), sent on every
    request as the `X-User-Id` header. Chat sessions are scoped to this id,
    so different visitors never see each other's chat history or memory.
  - `API_KEYS` are now ADMIN keys. A request carrying a valid `X-API-Key`
    (header) or `api_key` (query param) is treated as an admin request and
    can see/list data across ALL users (e.g. for training/review).

Provides:
  1. add_auth_middleware(app)  -- HTTP middleware; attaches request.state.user_id
                                   and request.state.is_admin to every request.
                                   Does NOT block normal users.
  2. require_admin_key         -- FastAPI Depends() for admin-only routes
                                   (e.g. /api/admin/*). Always enforced,
                                   regardless of AUTH_ENABLED.

Configuration (.env):
    AUTH_ENABLED=true                 # legacy flag, kept for back-compat (no-op for normal routes)
    API_KEYS=key1,key2,key3           # comma-separated ADMIN keys
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


def _extract_key(request: Request) -> Optional[str]:
    """Pull admin API key from X-API-Key header or ?api_key= query param."""
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    return key or None


def _extract_user_id(request: Request) -> str:
    """Pull the anonymous per-browser user id from X-User-Id header or ?user_id=."""
    uid = request.headers.get("X-User-Id") or request.query_params.get("user_id")
    uid = (uid or "").strip()
    return uid or "anon"


def _validate(key: Optional[str], valid_keys: frozenset[str]) -> bool:
    if not key:
        return False
    # Constant-time comparison against each valid key
    return any(secrets.compare_digest(key, vk) for vk in valid_keys)


def is_admin_request(request: Request) -> bool:
    """True if this request carries a valid admin API key."""
    valid_keys = _load_keys()
    if not valid_keys:
        return False
    return _validate(_extract_key(request), valid_keys)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# NOTE: Use @app.middleware("http") — NOT BaseHTTPMiddleware — so SSE /
# StreamingResponse endpoints (chat, voice, proactive) are not buffered.
#
# This middleware never blocks normal traffic. It simply stamps each request
# with `request.state.user_id` (anonymous per-browser identity, used to scope
# chat sessions/history) and `request.state.is_admin` (true if a valid
# X-API-Key/api_key admin key was supplied). Routes that expose data across
# users (e.g. /api/chat/sessions, /api/chat/{id}/history, /api/admin/*) use
# these to decide what to return.

def add_auth_middleware(app: FastAPI) -> None:
    """Register the identity/admin-tagging middleware on the FastAPI app."""

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        request.state.user_id = _extract_user_id(request)
        request.state.is_admin = is_admin_request(request)
        return await call_next(request)

    log.info("[auth] Identity middleware registered (per-browser user scoping + admin keys)")


# ---------------------------------------------------------------------------
# Per-route dependency for admin-only endpoints
# ---------------------------------------------------------------------------

async def require_admin_key(
    header_key: Optional[str] = Security(_header_scheme),
    query_key:  Optional[str] = Security(_query_scheme),
) -> str:
    """
    FastAPI dependency for admin-only routes (e.g. /api/admin/*).
    Always enforced — independent of AUTH_ENABLED — since these routes
    expose data belonging to every user.
    Usage:  @router.get("/secret", dependencies=[Depends(require_admin_key)])
    """
    valid_keys = _load_keys()
    key = header_key or query_key

    if not valid_keys:
        raise HTTPException(
            status_code=503,
            detail="Admin access not configured: set API_KEYS in .env",
        )

    if _validate(key, valid_keys):
        return key

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing admin API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


# Backwards-compat alias (old name used elsewhere)
require_api_key = require_admin_key
