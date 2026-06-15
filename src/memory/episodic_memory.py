"""
TRON-X Episodic Memory Agent  (Phase 13)
------------------------------------------
Stores and retrieves timestamped interaction episodes from ChromaDB.

Episode schema (stored as metadata):
  episode_id  : sha256 hash of content
  session_id  : conversation session
  user_msg    : raw user message (truncated)
  assistant   : raw assistant reply (truncated)
  summary     : LLM-extracted one-line summary  <-- this is what gets embedded
  topic       : LLM-extracted topic tag
  entities    : comma-separated named entities
  emotion     : user sentiment (positive/neutral/negative)
  timestamp   : unix epoch (float) for range filtering
  date        : YYYY-MM-DD for daily grouping

ChromaDB collection: "episodes"
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.core.logger import log
from src.memory.chroma_db import COL_EPISODES, get_chroma


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

async def _llm_extract(user_msg: str, assistant_reply: str) -> dict:
    """Ask LLM to extract summary, topic, entities, and sentiment."""
    prompt = (
        "Extract structured info from this conversation turn. "
        "Return ONLY a JSON object with keys: summary (one sentence), "
        "topic (2-4 word tag), entities (comma-separated names/places/products), "
        "emotion (positive|neutral|negative).\n\n"
        f"User: {user_msg[:400]}\nAssistant: {assistant_reply[:400]}"
    )
    try:
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()
        # Use a fresh throwaway session ID each time so episodic extractions
        # never accumulate history or appear in the session sidebar.
        import uuid as _uuid
        throwaway_id = f"__ep_{_uuid.uuid4().hex[:8]}__"
        result = await orch.chat(
            user_message=prompt,
            session_id=throwaway_id,
            intent="fast_chat",
            persona="jarvis",
            max_tokens=120,
            temperature=0.1,
        )
        # Clean up the throwaway session immediately
        try:
            orch.delete_session(throwaway_id)
        except Exception:
            pass
        reply = result.get("reply", "{}")
        # Strip code fences
        import re
        clean = re.sub(r"```(?:json)?", "", reply).strip().strip("`")
        data = json.loads(clean)
        return {
            "summary":  str(data.get("summary", f"{user_msg[:80]}"))[:200],
            "topic":    str(data.get("topic", "general"))[:60],
            "entities": str(data.get("entities", ""))[:200],
            "emotion":  str(data.get("emotion", "neutral"))[:20],
        }
    except Exception as e:
        log.warning(f"[episodic] LLM extraction failed ({e}), using fallback")
        return {
            "summary":  f"{user_msg[:120]}",
            "topic":    "general",
            "entities": "",
            "emotion":  "neutral",
        }


# ---------------------------------------------------------------------------
# EpisodicMemoryAgent
# ---------------------------------------------------------------------------

class EpisodicMemoryAgent:
    """Store and recall timestamped interaction episodes."""

    def __init__(self):
        self._chroma = get_chroma()

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------
    async def remember(
        self,
        user_msg: str,
        assistant_reply: str,
        session_id: str = "default",
        auto_extract: bool = True,
        topic: Optional[str] = None,
        entities: Optional[str] = None,
        emotion: Optional[str] = None,
    ) -> dict:
        """
        Store a conversation turn as an episode.
        If auto_extract=True, LLM summarizes/tags the episode automatically.
        """
        now = datetime.now(timezone.utc)
        ts  = now.timestamp()

        if auto_extract:
            extracted = await _llm_extract(user_msg, assistant_reply)
        else:
            extracted = {
                "summary":  f"{user_msg[:120]}",
                "topic":    topic or "general",
                "entities": entities or "",
                "emotion":  emotion or "neutral",
            }
        if topic:
            extracted["topic"] = topic
        if entities:
            extracted["entities"] = entities
        if emotion:
            extracted["emotion"] = emotion

        embed_text = (
            f"{extracted['summary']} | topic: {extracted['topic']} "
            f"| entities: {extracted['entities']}"
        )
        episode_id = hashlib.sha256(
            f"{session_id}{ts}{user_msg[:80]}".encode()
        ).hexdigest()[:16]

        metadata = {
            "episode_id": episode_id,
            "session_id": session_id,
            "user_msg":   user_msg[:500],
            "assistant":  assistant_reply[:500],
            "summary":    extracted["summary"],
            "topic":      extracted["topic"],
            "entities":   extracted["entities"],
            "emotion":    extracted["emotion"],
            "timestamp":  ts,
            "date":       now.strftime("%Y-%m-%d"),
        }

        await self._chroma.add(
            collection=COL_EPISODES,
            texts=[embed_text],
            metadatas=[metadata],
            ids=[episode_id],
        )
        log.info(f"[episodic] Stored episode {episode_id} | topic={extracted['topic']}")
        return {"stored": True, "episode_id": episode_id, **extracted}

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------
    async def recall(
        self,
        query: str,
        top_k: int = 5,
        days: Optional[int] = None,
        session_id: Optional[str] = None,
        topic: Optional[str] = None,
        min_score: float = 0.30,
    ) -> dict:
        """
        Semantic search over stored episodes.
        Optionally filter by time window, session, or topic.
        """
        where: dict = {}
        conditions = []

        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
            conditions.append({"timestamp": {"$gte": cutoff}})

        if session_id:
            conditions.append({"session_id": {"$eq": session_id}})

        if topic:
            conditions.append({"topic": {"$eq": topic}})

        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        try:
            hits = await self._chroma.search(
                collection=COL_EPISODES,
                query=query,
                top_k=top_k,
                where=where if where else None,
            )
        except Exception as e:
            log.error(f"[episodic] recall search failed: {e}")
            return {"query": query, "episodes": [], "error": str(e)}

        episodes = []
        for h in hits:
            if h.get("score", 0) < min_score:
                continue
            meta = h.get("metadata", {})
            episodes.append({
                "episode_id": meta.get("episode_id"),
                "session_id": meta.get("session_id"),
                "date":       meta.get("date"),
                "topic":      meta.get("topic"),
                "emotion":    meta.get("emotion"),
                "entities":   meta.get("entities"),
                "summary":    meta.get("summary"),
                "user_msg":   meta.get("user_msg"),
                "assistant":  meta.get("assistant"),
                "score":      round(h.get("score", 0), 3),
            })

        return {"query": query, "episodes": episodes, "count": len(episodes)}

    # ------------------------------------------------------------------
    # Daily / period summary
    # ------------------------------------------------------------------
    async def daily_summary(
        self,
        date: Optional[str] = None,
        persona: str = "jarvis",
    ) -> dict:
        """Fetch all episodes for a date (YYYY-MM-DD) and summarize."""
        target = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            col = self._chroma._cols[COL_EPISODES]
            results = col.get(where={"date": {"$eq": target}})
            metas = results.get("metadatas", [])
        except Exception as e:
            return {"error": str(e)}

        if not metas:
            return {"date": target, "summary": f"No episodes recorded on {target}.", "count": 0}

        lines = []
        for m in metas:
            lines.append(
                f"- [{m.get('topic','?')}] {m.get('summary','')} "
                f"(emotion: {m.get('emotion','?')})"
            )
        episode_text = "\n".join(lines)

        prompt = (
            f"Here are the interactions TRON-X had on {target}:\n\n"
            f"{episode_text}\n\n"
            "Write a concise daily recap: main topics discussed, tasks completed, "
            "user mood patterns, and anything worth remembering tomorrow."
        )
        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__episodic_daily__",
                intent="chat",
                persona=persona,
                max_tokens=600,
            )
            return {
                "date":    target,
                "summary": result.get("reply", ""),
                "count":   len(metas),
                "topics":  list({m.get("topic") for m in metas}),
            }
        except Exception as e:
            return {"date": target, "summary": episode_text, "count": len(metas), "error": str(e)}

    async def period_summary(
        self,
        days: int = 7,
        persona: str = "jarvis",
    ) -> dict:
        """Summarize all episodes over the last N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        try:
            col = self._chroma._cols[COL_EPISODES]
            results = col.get(where={"timestamp": {"$gte": cutoff}})
            metas = results.get("metadatas", [])
        except Exception as e:
            return {"error": str(e)}

        if not metas:
            return {"days": days, "summary": f"No episodes in the last {days} days.", "count": 0}

        # Group by date
        by_date: dict[str, list[str]] = {}
        for m in metas:
            d = m.get("date", "unknown")
            by_date.setdefault(d, []).append(
                f"  [{m.get('topic','?')}] {m.get('summary','')}"
            )

        lines = []
        for d in sorted(by_date):
            lines.append(f"{d}:")
            lines.extend(by_date[d])

        period_text = "\n".join(lines)

        prompt = (
            f"TRON-X interaction log for the last {days} days:\n\n"
            f"{period_text}\n\n"
            "Summarize: recurring themes, progress on goals, "
            "patterns in user behavior, and key decisions made."
        )
        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__episodic_period__",
                intent="chat",
                persona=persona,
                max_tokens=800,
            )
            return {
                "days":    days,
                "summary": result.get("reply", ""),
                "count":   len(metas),
                "topics":  list({m.get("topic") for m in metas}),
                "dates":   sorted(by_date.keys()),
            }
        except Exception as e:
            return {"days": days, "summary": period_text, "count": len(metas), "error": str(e)}

    # ------------------------------------------------------------------
    # List / inspect
    # ------------------------------------------------------------------
    async def list_episodes(
        self,
        days: int = 7,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        conditions = [{"timestamp": {"$gte": cutoff}}]
        if session_id:
            conditions.append({"session_id": {"$eq": session_id}})
        where = {"$and": conditions} if len(conditions) > 1 else conditions[0]

        try:
            col = self._chroma._cols[COL_EPISODES]
            results = col.get(where=where, limit=limit)
            metas = results.get("metadatas", [])
        except Exception as e:
            return {"error": str(e)}

        metas_sorted = sorted(metas, key=lambda m: m.get("timestamp", 0), reverse=True)
        return {"episodes": metas_sorted, "count": len(metas_sorted), "days": days}

    async def stats(self) -> dict:
        try:
            col = self._chroma._cols[COL_EPISODES]
            count = col.count()
            # Get date range of all episodes
            if count > 0:
                all_metas = col.get().get("metadatas", [])
                dates = sorted(m.get("date", "") for m in all_metas if m.get("date"))
                topics = {}
                for m in all_metas:
                    t = m.get("topic", "general")
                    topics[t] = topics.get(t, 0) + 1
                top_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:10]
                return {
                    "total_episodes": count,
                    "earliest_date":  dates[0] if dates else None,
                    "latest_date":    dates[-1] if dates else None,
                    "top_topics":     [{"topic": t, "count": c} for t, c in top_topics],
                }
            return {"total_episodes": 0}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Forget
    # ------------------------------------------------------------------
    async def forget_episode(self, episode_id: str) -> dict:
        try:
            col = self._chroma._cols[COL_EPISODES]
            col.delete(ids=[episode_id])
            return {"forgotten": True, "episode_id": episode_id}
        except Exception as e:
            return {"error": str(e)}

    async def forget_session(self, session_id: str) -> dict:
        try:
            col = self._chroma._cols[COL_EPISODES]
            results = col.get(where={"session_id": {"$eq": session_id}})
            ids = results.get("ids", [])
            if ids:
                col.delete(ids=ids)
            return {"forgotten": True, "session_id": session_id, "count": len(ids)}
        except Exception as e:
            return {"error": str(e)}

    async def forget_before(self, days: int, confirm: bool = False) -> dict:
        """Delete all episodes older than N days."""
        if not confirm:
            return {"error": "Set confirm=True to delete old episodes"}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        try:
            col = self._chroma._cols[COL_EPISODES]
            results = col.get(where={"timestamp": {"$lt": cutoff}})
            ids = results.get("ids", [])
            if ids:
                col.delete(ids=ids)
            return {"forgotten": True, "days": days, "count": len(ids)}
        except Exception as e:
            return {"error": str(e)}
