"""
TRON-X ChromaDB Manager
────────────────────────
Persistent local vector store.
Collections:
  • conversations  — chat history embeddings (for context recall)
  • documents      — ingested PDFs, notes, textbooks
  • knowledge      — manually added facts / user preferences

All writes are async-safe via asyncio.Lock.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from src.core.logger import log
from src.memory.embeddings import embed, embed_one

CHROMA_PATH = Path("memory/chroma")
CHROMA_PATH.mkdir(parents=True, exist_ok=True)

# Collection names
COL_CONVERSATIONS = "conversations"
COL_DOCUMENTS     = "documents"
COL_KNOWLEDGE     = "knowledge"
COL_EPISODES      = "episodes"

_TOP_K_DEFAULT = 5


async def _embed_one_async(text: str) -> list[float]:
    """Run CPU-heavy embedding off the event loop."""
    return await asyncio.to_thread(embed_one, text)


def _make_id(text: str) -> str:
    """Deterministic ID from content hash — prevents duplicate ingestion."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class ChromaManager:
    def __init__(self):
        self._client = chromadb.PersistentClient(
            path=str(CHROMA_PATH),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._lock = asyncio.Lock()
        self._cols: dict[str, chromadb.Collection] = {}
        self._init_collections()
        log.info("[chroma] ChromaDB ready ✓")

    def _init_collections(self) -> None:
        for name in (COL_CONVERSATIONS, COL_DOCUMENTS, COL_KNOWLEDGE, COL_EPISODES):
            self._cols[name] = self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        log.info(
            "[chroma] Collections: "
            + ", ".join(f"{n}({self._cols[n].count()})" for n in self._cols)
        )

    # ── Write ──────────────────────────────────────────────────────────────────

    async def add(
        self,
        collection: str,
        texts: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Embed and store texts. Returns list of IDs stored."""
        if not texts:
            return []

        # Dedup by content hash
        final_ids = ids or [_make_id(t + str(time.time())) for t in texts]
        metas = metadatas or [{} for _ in texts]

        vectors = await asyncio.to_thread(embed, texts)

        async with self._lock:
            col = self._cols[collection]
            await asyncio.to_thread(
                col.upsert,
                ids=final_ids,
                embeddings=vectors,
                documents=texts,
                metadatas=metas,
            )

        log.debug(f"[chroma] Stored {len(texts)} chunks → {collection}")
        return final_ids

    async def add_conversation_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        intent: str = "chat",
    ) -> None:
        """Store a Q&A pair for future recall."""
        combined = f"User: {user_msg}\nAssistant: {assistant_msg}"
        await self.add(
            collection=COL_CONVERSATIONS,
            texts=[combined],
            metadatas=[{
                "session_id": session_id,
                "intent":     intent,
                "timestamp":  time.time(),
                "type":       "conversation",
            }],
        )

    async def remember_fact(
        self,
        text: str,
        session_id: str | None = None,
        source: str = "user",
    ) -> str:
        """
        Permanently store a user-told fact in the 'knowledge' collection.

        Deduplicated by content hash (lowercased) so re-stating the same fact
        updates its timestamp instead of creating a duplicate. Facts stored
        here are surfaced in ALL future sessions via
        RAGPipeline.retrieve_knowledge() — independent of intent classification
        or should_use_rag() gating, so "remember X" is recalled forever.
        """
        fact_id = "fact_" + _make_id(text.strip().lower())
        await self.add(
            collection=COL_KNOWLEDGE,
            texts=[text],
            metadatas=[{
                "type":       "fact",
                "source":     source,
                "session_id": session_id or "",
                "timestamp":  time.time(),
            }],
            ids=[fact_id],
        )
        log.info(f"[chroma] Remembered fact: {text[:80]!r}")
        return fact_id

    # ── Search ─────────────────────────────────────────────────────────────────

    async def search(
        self,
        collection: str,
        query: str,
        top_k: int = _TOP_K_DEFAULT,
        where: Optional[dict] = None,
        min_score: float = 0.3,
    ) -> list[dict]:
        """
        Semantic search. Returns list of:
            {"text": str, "metadata": dict, "score": float}
        sorted by relevance (highest first).
        """
        query_vec = await _embed_one_async(query)

        kwargs: dict = {
            "query_embeddings": [query_vec],
            "n_results": min(top_k, max(1, self._cols[collection].count())),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = await asyncio.to_thread(self._cols[collection].query, **kwargs)

        hits = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score [0, 1]
            score = 1.0 - (dist / 2.0)
            if score >= min_score:
                hits.append({"text": doc, "metadata": meta, "score": round(score, 4)})

        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits

    async def search_all(
        self,
        query: str,
        top_k: int = _TOP_K_DEFAULT,
        min_score: float = 0.3,
    ) -> list[dict]:
        """Search across all collections and merge results."""
        all_hits = []
        for col_name in (COL_CONVERSATIONS, COL_DOCUMENTS, COL_KNOWLEDGE):
            if self._cols[col_name].count() == 0:
                continue
            hits = await self.search(col_name, query, top_k=top_k, min_score=min_score)
            for h in hits:
                h["collection"] = col_name
            all_hits.extend(hits)

        all_hits.sort(key=lambda x: x["score"], reverse=True)
        return all_hits[:top_k]

    async def find_facts(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
    ) -> list[dict]:
        """
        Search the 'knowledge' collection for remembered facts, including
        chroma IDs (needed by forget_fact). Returns:
            {"id": str, "text": str, "metadata": dict, "score": float}
        sorted by relevance (highest first).
        """
        col = self._cols[COL_KNOWLEDGE]
        if col.count() == 0:
            return []

        query_vec = await _embed_one_async(query)
        results = await asyncio.to_thread(
            col.query,
            query_embeddings=[query_vec],
            n_results=min(top_k, col.count()),
            include=["documents", "metadatas", "distances"],
        )

        hits = []
        for doc, meta, dist, _id in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
            results["ids"][0],
        ):
            score = 1.0 - (dist / 2.0)
            if score >= min_score:
                hits.append({"id": _id, "text": doc, "metadata": meta, "score": round(score, 4)})

        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits

    async def list_facts(self, limit: int = 100) -> list[dict]:
        """List all remembered facts, most recently stored first."""
        col = self._cols[COL_KNOWLEDGE]
        if col.count() == 0:
            return []
        results = col.get(limit=limit, include=["documents", "metadatas"])
        items = [
            {"id": _id, "text": doc, "metadata": meta}
            for _id, doc, meta in zip(
                results.get("ids", []),
                results.get("documents", []),
                results.get("metadatas", []),
            )
        ]
        items.sort(key=lambda x: x["metadata"].get("timestamp", 0), reverse=True)
        return items

    async def forget_fact(self, fact_id: str) -> bool:
        """Permanently delete a remembered fact by its chroma ID."""
        async with self._lock:
            self._cols[COL_KNOWLEDGE].delete(ids=[fact_id])
        return True

    # ── MMR re-ranking ────────────────────────────────────────

    def mmr_rerank(
        self,
        hits: list[dict],
        query: str,
        top_k: int = 5,
        lambda_param: float = 0.5,
    ) -> list[dict]:
        """
        Maximal Marginal Relevance: balances relevance vs. diversity.
        Prevents returning 5 nearly-identical chunks.
        lambda_param: 1.0 = pure relevance, 0.0 = pure diversity.
        """
        if len(hits) <= top_k:
            return hits

        import numpy as np

        candidate_vecs = [np.array(embed_one(h["text"])) for h in hits]

        selected_idx: list[int] = []
        remaining = list(range(len(hits)))

        while len(selected_idx) < top_k and remaining:
            best_idx = None
            best_score = -float("inf")

            for i in remaining:
                relevance = hits[i]["score"]

                if not selected_idx:
                    mmr_score = relevance
                else:
                    redundancy = max(
                        float(np.dot(candidate_vecs[i], candidate_vecs[j]) /
                              (np.linalg.norm(candidate_vecs[i]) * np.linalg.norm(candidate_vecs[j]) + 1e-9))
                        for j in selected_idx
                    )
                    mmr_score = lambda_param * relevance - (1 - lambda_param) * redundancy

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i

            selected_idx.append(best_idx)
            remaining.remove(best_idx)

        return [hits[i] for i in selected_idx]

    # ── Stats ────────────────────────────────────────

    def stats(self) -> dict:
        return {
            col: {"count": self._cols[col].count()}
            for col in self._cols
        }

    # ── Delete ────────────────────────────────────────

    async def delete_by_session(self, session_id: str) -> int:
        """Delete all conversation chunks for a session."""
        async with self._lock:
            col = self._cols[COL_CONVERSATIONS]
            results = col.get(where={"session_id": session_id})
            ids = results.get("ids", [])
            if ids:
                col.delete(ids=ids)
            return len(ids)

    async def delete_old_conversations(self, days: int = 30) -> int:
        """
        [Phase 28] Purge conversation turns older than `days` to free disk
        space under self-healing pressure. NEVER touches COL_KNOWLEDGE
        (remembered facts), COL_DOCUMENTS, or COL_EPISODES -- only the
        rolling chat-history collection, which is safe to prune.
        """
        cutoff = time.time() - days * 86400
        async with self._lock:
            col = self._cols[COL_CONVERSATIONS]
            try:
                results = col.get(where={"timestamp": {"$lt": cutoff}})
            except Exception as e:
                log.debug(f"[chroma] delete_old_conversations query failed: {e}")
                return 0
            ids = results.get("ids", [])
            if ids:
                col.delete(ids=ids)
                log.info(f"[chroma] Purged {len(ids)} conversation turn(s) older than {days}d")
            return len(ids)


# ── Singleton ────────────────────────────────────────────────────────────────────────────
_chroma: ChromaManager | None = None


def get_chroma() -> ChromaManager:
    global _chroma
    if _chroma is None:
        _chroma = ChromaManager()
    return _chroma

