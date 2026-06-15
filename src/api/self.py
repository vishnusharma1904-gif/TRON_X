"""
TRON-X Self-Model API  (Phase 38)
─────────────────────────────────
GET  /api/self/state     — full introspectable self-state
POST /api/self/reflect   — compose & persist a deep reflection now
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/self", tags=["self"])


@router.get("/state")
async def self_state():
    from src.intelligence.self_model import get_self_model
    return get_self_model().get()


@router.post("/reflect")
async def self_reflect(persona: str = "jarvis"):
    from src.intelligence.self_model import get_self_model
    return await get_self_model().deep_reflect(persona=persona)
