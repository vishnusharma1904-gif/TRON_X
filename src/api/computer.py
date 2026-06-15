"""
TRON-X Computer Control API  (Phase 4)
────────────────────────────────────────
Endpoints:
  GET  /api/computer/status          — availability + screen size
  POST /api/computer/screenshot      — current screen as base64 JPEG
  POST /api/computer/action          — execute a single desktop action
  GET  /api/computer/stream          — SSE stream of live screenshots
  POST /api/computer/ai_act          — AI-guided natural-language automation (SSE)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.logger import log
from src.agents.computer_agent import get_computer_agent
from src.agents.visual_computer import get_visual_computer

router = APIRouter(prefix="/api/computer", tags=["computer"])


# ── Models ────────────────────────────────────────────────────────────────────

class ActionRequest(BaseModel):
    action:  str
    params:  dict      = {}
    persona: str       = "jarvis"


class AiActRequest(BaseModel):
    instruction: str
    persona:     str  = "jarvis"
    max_steps:   int  = 6


class ScreenshotResponse(BaseModel):
    image:   str    # base64 JPEG
    width:   int
    height:  int
    ts:      float


# ── GET /api/computer/status ──────────────────────────────────────────────────

@router.get("/status")
async def status():
    ca = get_computer_agent()
    info = await ca.get_screen_size() if ca.is_available else None
    return {
        "available": ca.is_available,
        "screen":    {"width": info.width, "height": info.height} if info else None,
    }


# ── POST /api/computer/screenshot ─────────────────────────────────────────────

@router.post("/screenshot", response_model=ScreenshotResponse)
async def screenshot():
    ca = get_computer_agent()
    if not ca.is_available:
        raise HTTPException(503, "Computer agent unavailable — install pyautogui mss Pillow")
    b64  = await ca.screenshot()
    info = await ca.get_screen_size()
    return ScreenshotResponse(
        image=b64,
        width=info.width,
        height=info.height,
        ts=time.time(),
    )


# ── POST /api/computer/action ─────────────────────────────────────────────────

@router.post("/action")
async def execute_action(req: ActionRequest):
    ca = get_computer_agent()
    if not ca.is_available:
        raise HTTPException(503, "Computer agent unavailable")
    result = await ca.execute(req.action, **req.params)
    return {
        "success":    result.success,
        "action":     result.action,
        "details":    result.details,
        "error":      result.error,
        "latency_ms": result.latency_ms,
        "screenshot": result.screenshot,
    }


# ── GET /api/computer/stream ──────────────────────────────────────────────────

@router.get("/stream")
async def screenshot_stream(fps: float = 1.0):
    """
    SSE stream of live screenshots.
    fps controls capture rate (max 4, default 1).
    Each event: data: {"type":"frame","image":"<base64>","ts":<float>}
    """
    fps = max(0.2, min(fps, 4.0))
    interval = 1.0 / fps
    ca = get_computer_agent()

    async def _gen():
        if not ca.is_available:
            yield f"data: {json.dumps({'type':'error','message':'Computer agent unavailable'})}\n\n"
            return

        try:
            while True:
                b64 = await ca.screenshot_fast()
                if b64:
                    payload = json.dumps({"type": "frame", "image": b64, "ts": time.time()})
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"[computer/stream] error: {e}")
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── POST /api/computer/ai_act ─────────────────────────────────────────────────

@router.post("/ai_act")
async def ai_act(req: AiActRequest):
    """
    SSE stream — AI plans and executes a natural-language instruction.

    Event shapes:
      {type: "computer_start",   instruction, available, screen}
      {type: "computer_step",    step, phase, action, description, screenshot, success, error, done}
      {type: "computer_done",    steps_taken, result, screenshot, latency_ms}
      {type: "error",            message}
    """
    if not req.instruction.strip():
        raise HTTPException(400, "instruction must not be empty")

    vc = get_visual_computer()
    from src.intelligence.router import get_router
    router_inst = get_router()

    async def _gen():
        try:
            async for event in vc.stream(
                instruction=req.instruction,
                persona=req.persona,
                max_steps=min(req.max_steps, 10),
                router=router_inst,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            log.error(f"[computer/ai_act] {e}")
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
