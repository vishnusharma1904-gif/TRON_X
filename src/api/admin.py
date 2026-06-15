"""
TRON-X Admin API

Admin-only endpoints for reviewing chat history/memory across ALL users.
Every route here requires a valid admin API key (`X-API-Key` header or
`?api_key=` query param, see `.env` -> API_KEYS) via `require_admin_key`.

GET /api/admin/users                     -- list every browser/user with activity
GET /api/admin/sessions?user_id=<id>     -- list sessions (all, or for one user)
GET /api/admin/sessions/{session_id}/history -- full message history for any session
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional

from src.core.auth import require_admin_key
from src.intelligence.orchestrator import get_orchestrator

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


@router.get("/users")
async def list_users():
    """All distinct browser/users seen, with session counts and last activity."""
    return {"users": get_orchestrator().list_users()}


@router.get("/sessions")
async def list_all_sessions(user_id: Optional[str] = None):
    """List sessions across all users, or for a single user_id."""
    return {"sessions": get_orchestrator().list_sessions(user_id=user_id)}


@router.get("/sessions/{session_id}/history")
async def get_any_history(session_id: str):
    """Full message history for any session, regardless of owner."""
    orch = get_orchestrator()
    try:
        session = orch.get_session(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id":    session_id,
        "user_id":       session.get("user_id"),
        "persona":       session.get("persona", "jarvis"),
        "messages":      session["messages"],
        "message_count": len(session["messages"]),
    }
