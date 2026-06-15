"""
TRON-X RAG Pipeline
────────────────────
Retrieval-Augmented Generation coordinator.

Flow per request:
  1. Search ChromaDB (all collections) for top-K semantically similar chunks
  2. MMR re-rank to maximise relevance + diversity
  3. Format as context string for injection into system prompt
  4. Optionally store the conversation turn for future recall

Also wires into the Orchestrator: if RAG context is found,
it's injected into the system prompt before the LLM call.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from src.core.logger import log
from src.memory.chroma_db import COL_KNOWLEDGE, get_chroma

# Minimum similarity score to include a chunk (0–1)
MIN_SCORE = 0.40
TOP_K_RETRIEVE = 10   # fetch this many, then MMR-rerank down to TOP_K_FINAL
TOP_K_FINAL    = 5

# "Remembered facts" lookup — always run, regardless of intent/should_use_rag.
# Lower threshold + small top_k since these are short, user-curated facts and
# the collection is typically small.
KNOWLEDGE_MIN_SCORE = 0.35
KNOWLEDGE_TOP_K     = 3


def _format_context(hits: list[dict]) -> str:
    """Format retrieved chunks into a clean context block."""
    if not hits:
        return ""

    parts = []
    for i, h in enumerate(hits, 1):
        source = h["metadata"].get("source", "memory")
        score  = h["score"]
        parts.append(f"[{i}] (source: {source}, relevance: {score:.2f})\n{h['text']}")

    return "\n\n---\n\n".join(parts)


class RAGPipeline:
    def __init__(self):
        self._chroma = get_chroma()
        log.info("[rag] RAG pipeline ready ✓")

    async def retrieve(
        self,
        query: str,
        top_k: int = TOP_K_FINAL,
        min_score: float = MIN_SCORE,
        use_mmr: bool = True,
    ) -> tuple[str, list[dict]]:
        """
        Retrieve relevant context for a query.

        Returns:
            (context_string, raw_hits)
            context_string is empty if nothing useful was found.
        """
        # Search across all collections
        hits = await self._chroma.search_all(
            query=query,
            top_k=TOP_K_RETRIEVE,
            min_score=min_score,
        )

        if not hits:
            return "", []

        # MMR re-rank for diversity (CPU-heavy embeddings — off event loop)
        if use_mmr and len(hits) > top_k:
            hits = await asyncio.to_thread(
                self._chroma.mmr_rerank, hits, query, top_k,
            )
        else:
            hits = hits[:top_k]

        context = _format_context(hits)
        log.debug(f"[rag] Retrieved {len(hits)} chunks (top score={hits[0]['score']:.2f})")
        return context, hits

    async def retrieve_knowledge(
        self,
        query: str,
        top_k: int = KNOWLEDGE_TOP_K,
        min_score: float = KNOWLEDGE_MIN_SCORE,
    ) -> tuple[str, list[dict]]:
        """
        Always-on lookup of explicitly "remembered" facts (knowledge collection).

        Unlike retrieve(), this is NOT gated by should_use_rag()/intent — it's
        meant to run on every chat turn so that anything the user told TRON-X
        to remember is recalled in ANY future conversation, regardless of
        topic or trigger words.

        Returns (context_string, raw_hits); both empty if nothing matches or
        the knowledge collection is empty.
        """
        if self._chroma._cols[COL_KNOWLEDGE].count() == 0:
            return "", []

        hits = await self._chroma.search(COL_KNOWLEDGE, query, top_k=top_k, min_score=min_score)
        if not hits:
            return "", []

        parts = [f"[Remembered fact] {h['text']}" for h in hits]
        log.debug(f"[rag] Recalled {len(hits)} remembered fact(s) (top score={hits[0]['score']:.2f})")
        return "\n".join(parts), hits

    async def store_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        intent: str = "chat",
    ) -> None:
        """Store a completed conversation turn for future recall."""
        await self._chroma.add_conversation_turn(
            session_id=session_id,
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            intent=intent,
        )

    async def should_use_rag(self, query: str, intent: str) -> bool:
        """
        Decide whether to run RAG for this query.
        Skip for simple chat/IoT/system — use for academic/medical/research/coding.
        """
        rag_intents = {"academic", "medical", "research", "coding", "cad", "math", "reasoning"}
        if intent in rag_intents:
            return True

        # Also trigger if the query references memory ("remember", "last time", "you said")
        memory_triggers = [
            "remember", "last time", "you said", "we discussed",
            "earlier", "before", "previous", "that document", "the file",
            "i told you", "as i mentioned",
        ]
        query_lower = query.lower()
        return any(t in query_lower for t in memory_triggers)

    def stats(self) -> dict:
        return self._chroma.stats()


# ── Singleton ────────────────────────────────────────────────────────────────────────────
_rag: RAGPipeline | None = None


def get_rag() -> RAGPipeline:
    global _rag
    if _rag is None:
        _rag = RAGPipeline()
    return _rag
