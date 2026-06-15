"""
TRON-X Episodic Memory API  (Phase 13)
----------------------------------------
Prefix: /api/memory/episodic
Mounted under the existing /api/memory router namespace.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional

router = APIRouter(prefix="/api/memory/episodic", tags=["episodic-memory"])


class RememberReq(BaseModel):
    user_msg:        str
    assistant_reply: str
    session_id:      str = "default"
    auto_extract:    bool = True
    topic:           Optional[str] = None
    entities:        Optional[str] = None
    emotion:         Optional[str] = None

class RecallReq(BaseModel):
    query:      str
    top_k:      int = Field(default=5, ge=1, le=20)
    days:       Optional[int] = None
    session_id: Optional[str] = None
    topic:      Optional[str] = None
    min_score:  float = Field(default=0.30, ge=0.0, le=1.0)

class ForgetBeforeReq(BaseModel):
    days:    int = Field(..., ge=1)
    confirm: bool = False

class ForgetSessionReq(BaseModel):
    session_id: str


@router.post("/remember")
async def remember(req: RememberReq):
    """Store a conversation turn as an episodic memory."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().remember(
        user_msg=req.user_msg,
        assistant_reply=req.assistant_reply,
        session_id=req.session_id,
        auto_extract=req.auto_extract,
        topic=req.topic,
        entities=req.entities,
        emotion=req.emotion,
    )


@router.post("/recall")
async def recall(req: RecallReq):
    """Semantic search over stored episodes with optional time/session/topic filters."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().recall(
        query=req.query,
        top_k=req.top_k,
        days=req.days,
        session_id=req.session_id,
        topic=req.topic,
        min_score=req.min_score,
    )


@router.get("/episodes")
async def list_episodes(days: int = 7, session_id: Optional[str] = None, limit: int = 50):
    """List recent episodes with metadata."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().list_episodes(days, session_id, limit)


@router.get("/stats")
async def episodic_stats():
    """Total episode count, date range, top topics."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().stats()


@router.get("/summary/daily")
async def daily_summary(date: Optional[str] = None, persona: str = "jarvis"):
    """LLM digest of all episodes on a given date (defaults to today)."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().daily_summary(date, persona)


@router.get("/summary/period")
async def period_summary(days: int = 7, persona: str = "jarvis"):
    """LLM recap of the last N days: themes, patterns, decisions."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().period_summary(days, persona)


@router.delete("/episode/{episode_id}")
async def forget_episode(episode_id: str):
    """Delete a single episode by ID."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().forget_episode(episode_id)


@router.delete("/session")
async def forget_session(req: ForgetSessionReq):
    """Delete all episodes for a session."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().forget_session(req.session_id)


@router.delete("/before")
async def forget_before(req: ForgetBeforeReq):
    """Delete all episodes older than N days. Requires confirm=True."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    return await EpisodicMemoryAgent().forget_before(req.days, req.confirm)
