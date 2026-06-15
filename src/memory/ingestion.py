"""
TRON-X Document Ingestion Pipeline
────────────────────────────────────
Supports: PDF, TXT, MD, DOCX, URLs
Pipeline: load → chunk → deduplicate → embed → store in ChromaDB

Chunking strategy:
  - Sentence-aware splitting (respects paragraph breaks)
  - Configurable chunk_size / overlap
  - Metadata preservation (source, page, timestamp)
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.core.logger import log
from src.memory.chroma_db import COL_DOCUMENTS, COL_KNOWLEDGE, get_chroma

CHUNK_SIZE    = 500   # chars
CHUNK_OVERLAP = 80    # chars


# ── Chunk dataclass ───────────────────────────────────────────────────────────

@dataclass
class Chunk:
    text:     str
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


# ── Text splitter ─────────────────────────────────────────────────────────────

def _split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split on paragraph/sentence boundaries, then respect chunk_size.
    Never cuts in the middle of a sentence.
    """
    # Normalise whitespace
    text = re.sub(r"\n{3,}", "\n\n", text.strip())

    # Split on paragraph breaks first
    paragraphs = re.split(r"\n\n+", text)

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) <= chunk_size:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
                # Overlap: keep last `overlap` chars of previous chunk
                current = current[-overlap:].strip() + "\n\n" + para
            else:
                # Single paragraph exceeds chunk_size — split by sentences
                sentences = re.split(r"(?<=[.!?])\s+", para)
                for sent in sentences:
                    if len(current) + len(sent) <= chunk_size:
                        current = (current + " " + sent).strip()
                    else:
                        if current:
                            chunks.append(current)
                            current = current[-overlap:].strip() + " " + sent
                        else:
                            chunks.append(sent[:chunk_size])
                            current = ""

    if current:
        chunks.append(current)

    return [c for c in chunks if len(c.strip()) > 30]  # drop trivially short chunks


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _load_pdf(path: Path) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        return "\n\n".join(page.get_text() for page in doc)
    except ImportError:
        log.warning("[ingestion] PyMuPDF not installed — pip install pymupdf")
        return ""


def _load_docx(path: Path) -> str:
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        log.warning("[ingestion] python-docx not installed — pip install python-docx")
        return ""


def _load_url(url: str) -> str:
    try:
        import httpx
        from bs4 import BeautifulSoup
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except Exception as e:
        log.warning(f"[ingestion] URL load failed: {e}")
        return ""


# ── Main ingest function ──────────────────────────────────────────────────────

async def ingest_file(
    path: str | Path,
    collection: str = COL_DOCUMENTS,
    extra_metadata: Optional[dict] = None,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> int:
    """
    Ingest a file into ChromaDB.
    Returns number of chunks stored.
    """
    path = Path(path)
    if not path.exists():
        log.error(f"[ingestion] File not found: {path}")
        return 0

    ext = path.suffix.lower()
    loaders = {".txt": _load_txt, ".md": _load_txt, ".pdf": _load_pdf, ".docx": _load_docx}

    loader = loaders.get(ext)
    if not loader:
        log.warning(f"[ingestion] Unsupported file type: {ext}")
        return 0

    log.info(f"[ingestion] Loading {path.name}…")
    raw_text = loader(path)
    if not raw_text.strip():
        log.warning(f"[ingestion] Empty content from {path.name}")
        return 0

    chunks = _split_text(raw_text, chunk_size, overlap)
    log.info(f"[ingestion] Split into {len(chunks)} chunks")

    base_meta = {
        "source":    path.name,
        "source_path": str(path),
        "file_type": ext,
        "ingested_at": time.time(),
        **(extra_metadata or {}),
    }

    texts    = [c for c in chunks]
    metas    = [{**base_meta, "chunk_index": i} for i in range(len(chunks))]
    ids      = [hashlib.sha256((path.name + c).encode()).hexdigest()[:16] for c in chunks]

    chroma = get_chroma()
    await chroma.add(collection=collection, texts=texts, metadatas=metas, ids=ids)

    log.info(f"[ingestion] Stored {len(chunks)} chunks from {path.name} → {collection}")
    return len(chunks)


async def ingest_url(url: str, extra_metadata: Optional[dict] = None) -> int:
    """Ingest a web page into the documents collection."""
    log.info(f"[ingestion] Fetching {url}…")
    raw_text = _load_url(url)
    if not raw_text.strip():
        return 0

    chunks = _split_text(raw_text)
    base_meta = {
        "source": url,
        "file_type": "url",
        "ingested_at": time.time(),
        **(extra_metadata or {}),
    }

    chroma = get_chroma()
    await chroma.add(
        collection=COL_DOCUMENTS,
        texts=chunks,
        metadatas=[{**base_meta, "chunk_index": i} for i in range(len(chunks))],
    )
    return len(chunks)


async def ingest_text(
    text: str,
    source: str = "manual",
    collection: str = COL_KNOWLEDGE,
    extra_metadata: Optional[dict] = None,
) -> int:
    """Directly ingest raw text (e.g. user notes, facts)."""
    chunks = _split_text(text)
    meta = {"source": source, "ingested_at": time.time(), **(extra_metadata or {})}
    chroma = get_chroma()
    await chroma.add(
        collection=collection,
        texts=chunks,
        metadatas=[{**meta, "chunk_index": i} for i in range(len(chunks))],
    )
    return len(chunks)
