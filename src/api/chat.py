"""
TRON-X Chat API  v2

POST /api/chat
POST /api/chat/stream
POST /api/chat/vision
GET  /api/chat/sessions
GET  /api/chat/{id}/history
DELETE /api/chat/{id}
PATCH /api/chat/{id}/persona
PATCH /api/chat/{id}/title
DELETE /api/chat/{id}/delete
"""
import asyncio
import base64
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.core.logger import log
from src.intelligence.orchestrator import get_orchestrator

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message:     str   = Field(..., min_length=1, max_length=32000)
    session_id:  Optional[str]  = None
    intent:      str   = Field(
        default="auto",
        description=(
            "auto | chat | academic | medical | math | reasoning | "
            "coding | vision | iot | system | cad | research | creative"
        ),
    )
    persona:     str   = Field(default="jarvis", description="jarvis | friday")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens:  int   = Field(default=2048, ge=64, le=8192)
    stream:      bool  = False
    academic_mode: bool = False
    agent_mode:    bool = False
    attachments: Optional[list[dict]] = None


class ChatResponse(BaseModel):
    reply:             str
    model:             str
    session_id:        str
    intent:            str
    persona:           str
    confidence:        float
    tokens_used:       int
    latency_ms:        int
    thinking:          Optional[str]   = None
    error:             Optional[str]   = None
    emotion:           Optional[str]   = None   # detected user emotion
    emotion_intensity: Optional[float] = None   # 0.0 - 1.0
    telugu:            Optional[str]   = None   # dialect if Telugu detected
    language_profile:  Optional[dict]  = None
    voice_output_enabled: Optional[bool] = None
    academic_mode:     Optional[bool]  = None
    search:            Optional[dict]  = None
    citations:         Optional[list[dict]] = None
    self_model:        Optional[dict]  = None


class PersonaUpdate(BaseModel):
    persona: str = Field(..., description="jarvis | friday")


class TitleUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


# ---------------------------------------------------------------------------
# Analytics helper (fire-and-forget)
# ---------------------------------------------------------------------------

def _record_chat(result: dict, persona: str) -> None:
    """Queue a chat analytics event without blocking the response."""
    try:
        from src.analytics.collector import get_collector
        asyncio.create_task(
            get_collector().record_chat(
                session_id=result.get("session_id"),
                intent=result.get("intent", "unknown"),
                model=result.get("model", "unknown"),
                persona=persona,
                latency_ms=float(result.get("latency_ms", 0)),
                tokens=int(result.get("tokens_used", 0)),
                success=not bool(result.get("error")),
                prompt_tokens=int(result.get("prompt_tokens", 0)),
                completion_tokens=int(result.get("completion_tokens", 0)),
            )
        )
    except Exception:
        pass   # analytics must never break chat


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Main text chat -- auto-classifies intent and routes to best model."""
    orchestrator = get_orchestrator()
    effective_intent = req.intent
    if effective_intent == "auto":
        if req.agent_mode:
            effective_intent = "computer"
        elif req.academic_mode:
            effective_intent = "academic"
    extra_system = None
    image_data = None
    if req.attachments:
        from src.ingestion.attachments import Attachment, merge_for_prompt
        atts = [Attachment(**a) for a in req.attachments]
        text_block, image_data = merge_for_prompt(atts)
        if text_block:
            extra_system = f"The user attached the following files. Use their content to answer.\n\n{text_block}"
            
    result = await orchestrator.chat(
        user_message=req.message,
        session_id=req.session_id,
        intent="vision" if image_data and effective_intent == "auto" else effective_intent,
        persona=req.persona,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        stream=req.stream,
        extra_system=extra_system,
        image_data=image_data,
    )
    _record_chat(result, req.persona)
    # Phase 37: feed the live activity stream (never fatal)
    try:
        from src.core.event_bus import get_event_bus, EVT_AGENT_RESULT
        get_event_bus().publish(
            EVT_AGENT_RESULT, source="chat",
            intent=result.get("intent"), model=result.get("model"),
            latency_ms=result.get("latency_ms"),
            preview=str(result.get("reply", ""))[:120],
        )
    except Exception:
        pass
    return ChatResponse(**result)


@router.post("/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """
    Streaming text chat via Server-Sent Events.
    Events: meta -> text (many) -> done | error
    Format: data: <json>\\n\\n
    """
    orchestrator = get_orchestrator()
    effective_intent = req.intent
    if effective_intent == "auto":
        if req.agent_mode:
            effective_intent = "computer"
        elif req.academic_mode:
            effective_intent = "academic"

    extra_system = None
    image_data = None
    if req.attachments:
        from src.ingestion.attachments import Attachment, merge_for_prompt
        atts = [Attachment(**a) for a in req.attachments]
        text_block, image_data = merge_for_prompt(atts)
        if text_block:
            extra_system = f"The user attached the following files. Use their content to answer.\n\n{text_block}"

    async def _generate():
        try:
            async for chunk in orchestrator.chat_stream(
                user_message=req.message,
                session_id=req.session_id,
                intent="vision" if image_data and effective_intent == "auto" else effective_intent,
                persona=req.persona,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                extra_system=extra_system,
                image_data=image_data,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            log.error("[chat/stream] Unhandled error: %s", e)
            err = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@router.post("/vision", response_model=ChatResponse)
async def vision_chat(
    message:    str            = Form(...),
    session_id: Optional[str] = Form(default=None),
    persona:    str            = Form(default="jarvis"),
    file:       UploadFile     = File(...),
):
    """Multimodal chat: text + image. Routes to vision-capable model."""
    content = await file.read()
    mime    = file.content_type or "image/jpeg"
    b64     = base64.b64encode(content).decode()

    image_data = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]

    orchestrator = get_orchestrator()
    result = await orchestrator.chat(
        user_message=message,
        session_id=session_id,
        intent="vision",
        persona=persona,
        image_data=image_data,
    )
    _record_chat(result, persona)
    return ChatResponse(**result)


@router.get("/sessions")
async def list_sessions():
    return {"sessions": get_orchestrator().list_sessions()}


@router.get("/{session_id}/history")
async def get_history(session_id: str):
    try:
        session = get_orchestrator().get_session(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id":    session_id,
        "persona":       session.get("persona", "jarvis"),
        "messages":      session["messages"],
        "message_count": len(session["messages"]),
    }


@router.patch("/{session_id}/persona")
async def update_persona(session_id: str, body: PersonaUpdate):
    """Switch JARVIS to FRIDAY mid-session."""
    orch = get_orchestrator()
    try:
        orch.get_session(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    orch.set_persona(session_id, body.persona)
    return {"status": "updated", "session_id": session_id, "persona": body.persona}


@router.patch("/{session_id}/title")
async def rename_session(session_id: str, body: TitleUpdate):
    """Rename a session with a human-readable title."""
    orch = get_orchestrator()
    try:
        orch.get_session(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    orch.rename_session(session_id, body.title)
    return {"status": "renamed", "session_id": session_id, "title": body.title}


@router.delete("/{session_id}/delete")
async def delete_session(session_id: str):
    """Permanently delete a session from history."""
    deleted = get_orchestrator().delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


@router.delete("/{session_id}")
async def clear_session(session_id: str):
    get_orchestrator().clear_session(session_id)
    return {"status": "cleared", "session_id": session_id}
