"""
TRON-X Local Intent Cache & Semantic Command Routing  (Phase 22)
------------------------------------------------------------------
Skips the IntentClassifier's keyword + LLM round-trip for high-confidence
*repeat* commands (lights on/off, "what time is it", music controls, ...) by
matching the new message against prior classifications via embedding
cosine-similarity.

  SAFE_CACHEABLE_INTENTS
      Whitelist — the PRIMARY safety mechanism. Only intents in this set are
      ever written to or served from the cache, *regardless* of similarity.
      Anything that can send messages, touch files, run code, or write to a
      calendar (e.g. "system", "coding", "computer") is NEVER cached, because
      a near-miss similarity match could silently target the wrong file /
      contact / device.

  IntentCache
      SQLite-backed (memory/cache/intent_cache.sqlite). The whole table is
      small (<1000 rows expected) so similarity is brute-forced in pure
      Python over an in-memory copy — no ANN index needed.

        await cache.lookup(message)       -> Optional[CachedIntent]
        await cache.store(message, intent, resolved_action)
        await cache.evict_expired()       -> int   (TTL = INTENT_CACHE_TTL_DAYS)
        await cache.clear()               -> int   ("clear command cache")

  IoT entity-recheck (spec edge case)
      A cached "iot" entry's `resolved_action` may include an `entity_id`
      (from nl_mapper.parse_command's fast regex path). On lookup, if the
      best-matching entry is "iot" and carries an entity_id, that entity_id
      must still be a known device (src.iot.nl_mapper._DEVICE_ALIASES) or the
      hit is discarded — utterance similarity alone is never enough to
      dispatch a *device action* from cache.

  "clear command cache" / "reset routines"
      Manual cache wipe, wired into commands.py::try_handle_command the same
      way memory_commands.py wires "remember"/"forget".

  Fail-safe
      If the sqlite store can't be opened/initialized (e.g. some FUSE or
      network-mounted filesystems reject SQLite's locking model even for
      CREATE TABLE), the cache disables itself (IntentCache.enabled == False)
      instead of crashing startup. lookup()/store() become no-ops and
      evict_expired()/clear() return 0.

Integration points:
  - src/intelligence/intent.py          -- IntentClassifier.classify()
  - src/iot/nl_mapper.py                -- nl_to_ha_command() LLM-fallback hook
  - src/intelligence/commands.py        -- "clear command cache" command
  - src/core/config.py / .env.example   -- INTENT_CACHE_* settings
  - src/main.py                         -- startup + daily TTL eviction
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional

from src.core.config import get_settings
from src.core.logger import log

# ---------------------------------------------------------------------------
# Whitelist — see module docstring. The spec's example names
# (iot_light/iot_music/time_query/weather_query/...) map onto this codebase's
# actual top-level intent taxonomy (src/intelligence/intent.py::_INTENT_PATTERNS)
# as "iot" and "chat" respectively — "what time is it" / "what's the weather"
# fall through keyword classification to "chat", and all smart-home commands
# classify as "iot".
# ---------------------------------------------------------------------------
SAFE_CACHEABLE_INTENTS: set[str] = {"chat", "iot"}

# Minimum *fresh* classification confidence required before a result is
# eligible for storage.
#
# Reconciling spec vs. codebase: the handoff spec says "confidence >= 0.9".
# In this codebase, _keyword_classify() reaches 0.90 only with 4+ keyword
# hits for a single intent; everyday IoT phrasings like "turn on the lights"
# / "switch off the lights" score 0.75-0.85 and never reach the LLM stage
# (use_llm requires kw_confidence < 0.70). A literal >=0.9 gate would make
# the cache almost never fire for the spec's own headline example. We use
# 0.75 -- the keyword classifier's "single confident match" floor -- as the
# storage gate, with SAFE_CACHEABLE_INTENTS remaining the primary safety
# boundary (per the spec's own framing: "Whitelist is the safety mechanism").
MIN_CONFIDENCE_TO_STORE = 0.75

_DEFAULT_DB_PATH = "memory/cache/intent_cache.sqlite"


@dataclass
class CachedIntent:
    intent: str
    resolved_action: dict
    similarity: float


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _default_embed(texts: list[str]) -> list[list[float]]:
    """Lazy import so importing intent_cache never requires sentence-transformers
    unless the cache is actually used (and is enabled)."""
    from src.memory.embeddings import embed
    return embed(texts)


class IntentCache:
    """
    SQLite-backed semantic cache mapping prior user utterances to
    (intent, resolved_action) pairs, keyed by embedding cosine-similarity.

    `embed_fn` / `enabled` / `threshold` / `ttl_days` are injectable so the
    cache logic (similarity, TTL, whitelist, entity-recheck) can be unit
    tested without sentence-transformers. Production code (get_intent_cache())
    leaves them at their defaults, which read from Settings.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
        enabled: Optional[bool] = None,
        threshold: Optional[float] = None,
        ttl_days: Optional[int] = None,
    ):
        settings = get_settings()
        self._enabled = settings.intent_cache_enabled if enabled is None else enabled
        self._threshold = settings.intent_cache_sim_threshold if threshold is None else threshold
        self._ttl_days = settings.intent_cache_ttl_days if ttl_days is None else ttl_days
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._embed = embed_fn or _default_embed
        self._entries: list[dict] = []
        if self._enabled:
            try:
                self._init_db()
                self._load_all()
            except sqlite3.Error as e:
                # Fail-safe: some filesystems (FUSE/virtiofs mounts, certain
                # network drives) reject SQLite's locking/journal model even
                # for CREATE TABLE. Degrade to a no-op cache instead of
                # crashing app startup -- see the `enabled` property below.
                log.warning(
                    f"[intent_cache] disabling -- could not open '{self._db_path}': {e}"
                )
                self._enabled = False
                self._entries = []

    @property
    def enabled(self) -> bool:
        """True if the cache is configured on AND its sqlite store opened
        successfully. False means lookup()/store() are no-ops and
        evict_expired()/clear() return 0 -- see __init__'s fail-safe."""
        return self._enabled

    # -- internal ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intent_cache (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_norm    TEXT NOT NULL,
                    intent          TEXT NOT NULL,
                    resolved_action TEXT NOT NULL DEFAULT '{}',
                    embedding       TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    last_used_at    REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_intent_cache_norm "
                "ON intent_cache(message_norm)"
            )
            conn.commit()

    def _load_all(self) -> None:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, message_norm, intent, resolved_action, embedding, "
                    "created_at, last_used_at FROM intent_cache"
                ).fetchall()
        except sqlite3.Error as e:
            log.warning(f"[intent_cache] load failed: {e}")
            rows = []
        self._entries = [
            {
                "id": r[0],
                "message_norm": r[1],
                "intent": r[2],
                "resolved_action": json.loads(r[3] or "{}"),
                "embedding": json.loads(r[4] or "[]"),
                "created_at": r[5],
                "last_used_at": r[6],
            }
            for r in rows
        ]

    @staticmethod
    def _norm(message: str) -> str:
        return " ".join((message or "").strip().lower().split())

    def _touch(self, row_id: int) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE intent_cache SET last_used_at = ? WHERE id = ?",
                    (time.time(), row_id),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    # -- public API ------------------------------------------------------------

    async def lookup(self, message: str) -> Optional[CachedIntent]:
        """
        Embed `message` and find the nearest neighbour by cosine similarity.

        Returns None when: disabled, cache empty, best similarity is below
        threshold, the best entry is expired (TTL), its intent isn't in
        SAFE_CACHEABLE_INTENTS, embeddings are unavailable, or (for "iot")
        its cached entity_id is no longer a known device.
        """
        if not self._enabled or not self._entries:
            return None

        try:
            vec = (await asyncio.to_thread(self._embed, [message]))[0]
        except ModuleNotFoundError as e:
            log.debug(f"[intent_cache] embeddings unavailable, skipping lookup: {e}")
            return None
        except Exception as e:
            log.warning(f"[intent_cache] embed failed during lookup: {e}")
            return None

        best, best_sim = None, -1.0
        for entry in self._entries:
            sim = _cosine(vec, entry["embedding"])
            if sim > best_sim:
                best, best_sim = entry, sim

        if best is None or best_sim < self._threshold:
            return None

        if best["intent"] not in SAFE_CACHEABLE_INTENTS:
            # Defensive: store() should never persist a non-whitelisted
            # intent, but never *serve* one from cache either way.
            return None

        age_days = (time.time() - best["created_at"]) / 86400.0
        if age_days > self._ttl_days:
            return None

        # IoT entity-recheck (spec edge case): a cached device action is only
        # served if that device is still a known entity. Utterance similarity
        # alone is not enough to dispatch a device command from cache.
        resolved_action = best["resolved_action"] or {}
        entity_id = resolved_action.get("entity_id")
        if best["intent"] == "iot" and entity_id:
            try:
                from src.iot.nl_mapper import _DEVICE_ALIASES
                if entity_id not in _DEVICE_ALIASES.values():
                    return None
            except Exception:
                # If nl_mapper can't be imported for some reason, fail safe
                # by NOT serving a device action from cache.
                return None

        self._touch(best["id"])
        return CachedIntent(
            intent=best["intent"],
            resolved_action=resolved_action,
            similarity=best_sim,
        )

    async def store(self, message: str, intent: str, resolved_action: Optional[dict] = None) -> None:
        """
        Persist a fresh classification for future semantic lookups.

        No-op if disabled or `intent` isn't in SAFE_CACHEABLE_INTENTS -- the
        whitelist is enforced here too, not just at call sites.
        """
        if not self._enabled or intent not in SAFE_CACHEABLE_INTENTS:
            return

        try:
            vec = (await asyncio.to_thread(self._embed, [message]))[0]
        except ModuleNotFoundError as e:
            log.debug(f"[intent_cache] embeddings unavailable, skipping store: {e}")
            return
        except Exception as e:
            log.warning(f"[intent_cache] embed failed during store: {e}")
            return

        norm = self._norm(message)
        now = time.time()
        action_json = json.dumps(resolved_action or {})
        emb_json = json.dumps(vec)

        with self._connect() as conn:
            # Replace any existing entry for the exact same normalised
            # message so repeated identical phrasings don't bloat the table.
            conn.execute("DELETE FROM intent_cache WHERE message_norm = ?", (norm,))
            conn.execute(
                "INSERT INTO intent_cache "
                "(message_norm, intent, resolved_action, embedding, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (norm, intent, action_json, emb_json, now, now),
            )
            conn.commit()
        self._load_all()
        log.debug(f"[intent_cache] stored '{message[:40]}…' -> {intent} {resolved_action or ''}")

    async def evict_expired(self) -> int:
        """Delete entries older than INTENT_CACHE_TTL_DAYS. Returns count removed."""
        if not self._enabled:
            return 0
        cutoff = time.time() - self._ttl_days * 86400.0
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM intent_cache WHERE created_at < ?", (cutoff,))
            removed = cur.rowcount
            conn.commit()
        if removed:
            self._load_all()
            log.info(f"[intent_cache] Evicted {removed} expired entr{'y' if removed == 1 else 'ies'}")
        return removed or 0

    async def clear(self) -> int:
        """Manual 'clear command cache' -- wipes the whole table. Returns rows removed."""
        if not self._enabled:
            return 0
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM intent_cache")
            removed = cur.rowcount
            conn.commit()
        self._entries = []
        return removed or 0


@lru_cache(maxsize=1)
def get_intent_cache() -> IntentCache:
    return IntentCache()


# ---------------------------------------------------------------------------
# "clear command cache" / "reset routines" command
# (mirrors memory_commands.py's parse_remember_command / handle_remember
#  pattern -- a small local lead-in stripper avoids a module-level import
#  cycle with commands.py, same rationale as memory_commands.py.)
# ---------------------------------------------------------------------------

_LEADIN = re.compile(
    r"^\s*(?:please|pls|plz|hey|hi|hello|ok|okay|yo|jarvis|friday|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|i\s+want\s+to|i'?d\s+like\s+to|"
    r"i\s+need\s+to|let'?s|kindly|go\s+ahead\s+and)\b[\s,]*",
    re.I,
)

# Deliberately uses clear/reset/flush (not "forget") so this never collides
# with memory_commands.py's `_FORGET` ("^forget...") pattern.
_CLEAR_CACHE = re.compile(
    r"^(?:clear|reset|flush)\s+(?:the\s+|my\s+)?"
    r"(?:command\s+cache|intent\s+cache|cached\s+commands?|routines?)[.!?]*$",
    re.I,
)


def _strip_leadins(text: str) -> str:
    out = (text or "").strip()
    for _ in range(4):
        new = _LEADIN.sub("", out, count=1).strip()
        if new == out:
            break
        out = new
    return out


def parse_clear_cache_command(message: str) -> bool:
    """True if `message` is a 'clear command cache' / 'reset routines' style command."""
    if not message or not message.strip():
        return False
    return bool(_CLEAR_CACHE.match(_strip_leadins(message)))


async def handle_clear_cache(persona: str = "jarvis") -> str:
    """Wipe the local intent cache. Wired into commands.py::try_handle_command."""
    n = await get_intent_cache().clear()
    if persona == "friday":
        if n:
            return f"Cleared {n} cached command{'s' if n != 1 else ''}. Fresh start!"
        return "Nothing to clear -- the command cache was already empty."
    if n:
        return f"Done, sir. Cleared {n} cached command{'s' if n != 1 else ''} from the intent cache."
    return "The command cache was already empty, sir."
