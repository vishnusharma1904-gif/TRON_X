"""
TRON-X Embedding Engine
────────────────────────
CPU-optimised SentenceTransformers wrapper.
Model: all-MiniLM-L6-v2  (22 MB, 384-dim, fast on CPU)

Singleton pattern — model loaded once on first use.

[Phase 29] Embeddings stay 100% local regardless of the LLM-*generation*
fallback added to src/intelligence/router.py. `settings.embedding_backend`
defaults to "sentence_transformers" (this module, unchanged). Setting it to
"ollama" optionally routes embed()/embed_one() to a local Ollama embedding
model (settings.ollama_model, e.g. "nomic-embed-text") via
{ollama_base_url}/api/embeddings -- for users who want larger embeddings
without extra pip deps. NOTE: vector dimensionality then depends on the
chosen Ollama model and is NOT guaranteed to be EMBED_DIM=384; a ChromaDB
collection built under one backend is not compatible with the other.
"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import List

import httpx

from src.core.config import get_settings
from src.core.logger import log

MODEL_ID = "all-MiniLM-L6-v2"
EMBED_DIM = 384

# [Fix] Hugging Face cache was relocated from the now-missing E:\ drive to
# D:\updated e drive (which already contains a populated "hub" folder).
# Force the HF cache env vars BEFORE sentence_transformers/huggingface_hub is
# ever imported (see _get_model below), so model loading doesn't fail with
# FileNotFoundError: 'E:\\' regardless of what the system-level env vars say.
_HF_CACHE_ROOT = Path(r"D:\updated e drive")
if _HF_CACHE_ROOT.exists():
    os.environ["HF_HOME"] = str(_HF_CACHE_ROOT)
    os.environ["HF_HUB_CACHE"] = str(_HF_CACHE_ROOT / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(_HF_CACHE_ROOT / "hub")
else:
    log.warning(
        f"[embeddings] HF cache dir {_HF_CACHE_ROOT} not found -- "
        "leaving HF cache env vars untouched (model load may fail)."
    )

settings = get_settings()


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import SentenceTransformer
    log.info(f"[embeddings] Loading {MODEL_ID}…")
    model = SentenceTransformer(MODEL_ID)
    log.info("[embeddings] Model ready ✓")
    return model


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """[Phase 29, optional] Embed via a local Ollama embedding model.

    Synchronous (httpx.Client), since embed()/embed_one() are called from
    sync contexts throughout the codebase.
    """
    vectors: list[list[float]] = []
    with httpx.Client(timeout=30.0) as client:
        for text in texts:
            resp = client.post(
                f"{settings.ollama_base_url}/api/embeddings",
                json={"model": settings.ollama_model, "prompt": text},
            )
            resp.raise_for_status()
            vectors.append(resp.json()["embedding"])
    return vectors


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts.

    Returns 384-dim float vectors via SentenceTransformers by default
    (settings.embedding_backend == "sentence_transformers"). If
    embedding_backend == "ollama", routes to a local Ollama embedding model
    instead -- see module docstring for the dimensionality caveat.
    """
    if not texts:
        return []
    if settings.embedding_backend == "ollama":
        return _embed_ollama(texts)
    model = _get_model()
    vectors = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
    return vectors.tolist()


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
