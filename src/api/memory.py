"""
Memory & RAG API endpoints
POST /api/memory/ingest/file   — upload + ingest a document
POST /api/memory/ingest/text   — ingest raw text / notes
POST /api/memory/ingest/url    — ingest a web page
POST /api/memory/search        — semantic search
GET  /api/memory/stats         — collection stats
DELETE /api/memory/session/{id} — clear session memory
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

from src.core.logger import log
from src.memory.chroma_db import COL_DOCUMENTS, COL_KNOWLEDGE, get_chroma
from src.memory.ingestion import ingest_file, ingest_text, ingest_url
from src.memory.rag import get_rag
from src.memory.supabase_client import get_supabase

import shutil, tempfile
from pathlib import Path

router = APIRouter(prefix="/api/memory", tags=["memory"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class IngestTextRequest(BaseModel):
    text:       str  = Field(..., min_length=10)
    source:     str  = Field(default="manual")
    collection: str  = Field(default="knowledge")

class IngestURLRequest(BaseModel):
    url:    str
    source: Optional[str] = None

class SearchRequest(BaseModel):
    query:     str   = Field(..., min_length=2)
    top_k:     int   = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    use_mmr:   bool  = True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/ingest/file")
async def ingest_file_endpoint(
    file:       UploadFile = File(...),
    collection: str        = Form(default="documents"),
):
    """Upload and ingest a PDF, TXT, MD, or DOCX file."""
    allowed = {".pdf", ".txt", ".md", ".docx"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file type '{suffix}'. Allowed: {allowed}")

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        n = await ingest_file(
            path=tmp_path,
            collection=collection,
            extra_metadata={"original_name": file.filename},
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if n == 0:
        raise HTTPException(422, "Could not extract text from file")

    return {"status": "ingested", "chunks": n, "file": file.filename, "collection": collection}


@router.post("/ingest/text")
async def ingest_text_endpoint(req: IngestTextRequest):
    """Ingest raw text directly (notes, facts, preferences)."""
    n = await ingest_text(text=req.text, source=req.source, collection=req.collection)
    return {"status": "ingested", "chunks": n, "source": req.source}


@router.post("/ingest/url")
async def ingest_url_endpoint(req: IngestURLRequest):
    """Fetch and ingest a web page."""
    n = await ingest_url(
        url=req.url,
        extra_metadata={"source_label": req.source or req.url},
    )
    if n == 0:
        raise HTTPException(422, "Could not extract content from URL")
    return {"status": "ingested", "chunks": n, "url": req.url}


@router.post("/search")
async def search_memory(req: SearchRequest):
    """Semantic search across all memory collections."""
    rag = get_rag()
    context, hits = await rag.retrieve(
        query=req.query,
        top_k=req.top_k,
        min_score=req.min_score,
        use_mmr=req.use_mmr,
    )
    return {
        "query":   req.query,
        "hits":    hits,
        "context": context,
        "count":   len(hits),
    }


@router.get("/stats")
async def memory_stats():
    chroma = get_chroma()
    supabase = get_supabase()
    return {
        "chroma":   chroma.stats(),
        "supabase": supabase.status(),
    }


@router.delete("/session/{session_id}")
async def clear_session_memory(session_id: str):
    chroma = get_chroma()
    deleted = await chroma.delete_by_session(session_id)
    return {"status": "cleared", "session_id": session_id, "chunks_deleted": deleted}
