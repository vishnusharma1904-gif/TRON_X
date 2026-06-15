"""
TRON-X Memory Consolidation  (Phase 37)
───────────────────────────────────────
Nightly job that turns the ever-growing episodic log into durable
long-term memory — the way sleep consolidates human memory:

  1. Summarize yesterday's episodes (existing period_summary()).
  2. Promote recurring topics into the permanent 'knowledge' collection
     (deduplicated by content hash via remember_fact()).
  3. Prune episodes older than the retention window (forget_before()).
  4. Publish EVT_CONSOLIDATION with the stats.

Everything reuses existing, tested memory primitives — this module only
orchestrates them.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from src.core.config import get_settings
from src.core.logger import log
from src.core.event_bus import get_event_bus, EVT_CONSOLIDATION

settings = get_settings()

_MIN_TOPIC_COUNT = 2     # topic must recur to be promoted
_PROMOTE_LIMIT = 5       # max facts promoted per night


async def consolidate(retention_days: Optional[int] = None,
                      prune: Optional[bool] = None) -> dict:
    """Run one consolidation pass. Safe to call ad-hoc via the API."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    from src.memory.chroma_db import get_chroma

    retention = retention_days if retention_days is not None \
        else settings.consolidation_retention_days
    do_prune = prune if prune is not None \
        else settings.consolidation_prune_enabled

    epi = EpisodicMemoryAgent()
    stats: dict = {"summary": None, "promoted": [], "pruned": 0, "errors": []}

    # 1. Summarize the last day
    try:
        summary = await epi.period_summary(days=1)
        stats["summary"] = summary.get("summary") or summary.get("error")
    except Exception as e:
        stats["errors"].append(f"summary: {e}")

    # 2. Promote recurring topics → knowledge
    try:
        recent = await epi.list_episodes(days=7, limit=200)
        topics = Counter(
            (e.get("topic") or "").strip().lower()
            for e in recent.get("episodes") or []
            if (e.get("topic") or "").strip()
        )
        chroma = get_chroma()
        for topic, count in topics.most_common(_PROMOTE_LIMIT):
            if count < _MIN_TOPIC_COUNT:
                break
            fact = (f"Recurring interest: the user has discussed "
                    f"'{topic}' {count} times in the past week.")
            await chroma.remember_fact(fact, source="consolidation")
            stats["promoted"].append({"topic": topic, "count": count})
    except Exception as e:
        stats["errors"].append(f"promote: {e}")

    # 3. Prune old episodes
    if do_prune and retention > 0:
        try:
            result = await epi.forget_before(days=retention, confirm=True)
            stats["pruned"] = result.get("count", 0)
        except Exception as e:
            stats["errors"].append(f"prune: {e}")

    get_event_bus().publish(
        EVT_CONSOLIDATION, source="consolidation",
        promoted=len(stats["promoted"]), pruned=stats["pruned"],
        errors=len(stats["errors"]),
    )
    log.info(f"[consolidation] promoted={len(stats['promoted'])} "
             f"pruned={stats['pruned']} errors={len(stats['errors'])}")
    return stats
