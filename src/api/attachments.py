"""
TRON-X Attachments API  (Phase 38)

POST /api/attachments/process       — extract content from one or more files
POST /api/attachments/chat          — chat with any attachments as context
POST /api/attachments/ingest        — extract + store in ChromaDB knowledge base
GET  /api/attachments/supported     — list supported types per kind
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from src.core.logger import log
from src.ingestion.attachments import (
    AttachmentProcessor,
    merge_for_prompt,
    process_many,
)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 MB per file
MAX_FILES      = 10


async def _read_uploads(files: list[UploadFile]) -> list[tuple[bytes, str]]:
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Too many files (max {MAX_FILES})")
    out: list[tuple[bytes, str]] = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{f.filename}: exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit")
        out.append((raw, f.filename or "upload.bin"))
    return out


@router.get("/supported")
async def supported_types():
    """List supported extensions grouped by kind."""
    from src.ingestion import attachments as a
    return {
        "image":    sorted(a._IMAGE_EXT),
        "audio":    sorted(a._AUDIO_EXT),
        "video":    sorted(a._VIDEO_EXT),
        "document": sorted(a._DOC_EXT),
        "archive":  sorted(a._ARCHIVE_EXT),
        "code":     sorted(a._CODE_EXT),
        "data":     sorted(a._DATA_EXT),
        "text":     sorted(a._PLAINTEXT_EXT),
    }


@router.post("/process")
async def process(files: list[UploadFile] = File(...), full: bool = False):
    """Extract normalised content from uploaded files (no LLM call)."""
    pairs = await _read_uploads(files)
    atts = process_many(pairs)
    return {"count": len(atts), "attachments": [a.to_dict(include_full=full) for a in atts]}


@router.post("/chat")
async def chat_with_attachments(
    message:    str = Form(...),
    files:      list[UploadFile] = File(...),
    session_id: Optional[str] = Form(None),
    persona:    str = Form("jarvis"),
    intent:     str = Form("auto"),
):
    """
    Multimodal chat: text + any mix of attachments. Documents/code/data are
    injected as context text; images ride along as image_data blocks.
    """
    pairs = await _read_uploads(files)
    atts = process_many(pairs)
    text_block, image_data = merge_for_prompt(atts)

    extra_system = None
    if text_block:
        extra_system = (
            "The user attached the following files. Use their content to answer.\n\n"
            + text_block
        )

    from src.intelligence.orchestrator import get_orchestrator
    orch = get_orchestrator()
    result = await orch.chat(
        user_message=message,
        session_id=session_id,
        intent="vision" if image_data and intent == "auto" else intent,
        persona=persona,
        image_data=image_data,
        extra_system=extra_system,
        max_tokens=2048,
    )
    result["attachments"] = [a.to_dict() for a in atts]
    return result


@router.post("/ingest")
async def ingest_to_memory(files: list[UploadFile] = File(...)):
    """Extract text from uploads and store it in the ChromaDB knowledge base."""
    pairs = await _read_uploads(files)
    atts = process_many(pairs)

    from src.memory.ingestion import ingest_text
    stored = []
    for a in atts:
        if not a.text.strip():
            stored.append({"filename": a.filename, "chunks": 0, "note": a.note})
            continue
        try:
            n = await ingest_text(a.text, source=a.filename)
            stored.append({"filename": a.filename, "chunks": n})
        except Exception as e:
            log.warning(f"[attachments] ingest failed for {a.filename}: {e}")
            stored.append({"filename": a.filename, "chunks": 0, "error": str(e)})
    return {"count": len(atts), "stored": stored}
