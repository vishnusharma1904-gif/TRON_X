"""
TRON-X Analytics Collector  --  Phase 17

Persistent, non-blocking analytics backend.
Storage: SQLite at ~/.tronx/analytics.db via aiosqlite.

All write methods are fire-and-forget (called via asyncio.create_task)
so they add zero latency to real requests.

Schema
------
requests    -- every HTTP call (from middleware)
chat_events -- every /api/chat call (intent, model, persona, latency)
agent_events-- every agent invocation (agent name, latency)
error_events-- 5xx responses and caught exceptions
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from src.core.logger import log

_DB_PATH = Path.home() / ".tronx" / "analytics.db"

# [Phase 34] Per-model/provider pricing for the Cost & Usage dashboard.
_PRICING_PATH: Path = Path("config/pricing.json")
_pricing_cache: Optional[dict] = None

# [Phase 34] How many recent Phase 28 self-healing diagnostics entries to
# scan for circuit-breaker events (Phase 28's ring buffer caps at 2000).
_MAX_DIAG_SCAN = 500

# Regex to normalise path params so /api/chat/abc-123 -> /api/chat/{id}
_UUID_RE   = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
_NUM_RE    = re.compile(r'/\d+(?=/|$)')
_HASH32_RE = re.compile(r'/[0-9a-f]{32,}(?=/|$)', re.I)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    method      TEXT    NOT NULL,
    endpoint    TEXT    NOT NULL,
    status_code INTEGER NOT NULL,
    latency_ms  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    session_id  TEXT,
    intent      TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    persona     TEXT    NOT NULL DEFAULT 'jarvis',
    latency_ms  REAL    NOT NULL,
    tokens      INTEGER DEFAULT 0,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    success     INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agent_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    agent_name  TEXT    NOT NULL,
    latency_ms  REAL    NOT NULL,
    success     INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS error_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    endpoint    TEXT    DEFAULT '',
    error_type  TEXT    DEFAULT '',
    message     TEXT    DEFAULT '',
    model       TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_req_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_chat_ts   ON chat_events(ts);
CREATE INDEX IF NOT EXISTS idx_agent_ts  ON agent_events(ts);
CREATE INDEX IF NOT EXISTS idx_error_ts  ON error_events(ts);
"""


def _norm(path: str) -> str:
    """Normalise a URL path so path-param variants collapse to one key."""
    p = _UUID_RE.sub('{id}', path)
    p = _HASH32_RE.sub('/{hash}', p)
    p = _NUM_RE.sub('/{id}', p)
    return p


def _ensure_aiosqlite():
    try:
        import aiosqlite
        return aiosqlite
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aiosqlite",
             "--break-system-packages", "--quiet"], check=True
        )
        import aiosqlite
        return aiosqlite


# ---------------------------------------------------------------------------
# AnalyticsCollector
# ---------------------------------------------------------------------------

class AnalyticsCollector:
    """
    Async SQLite-backed analytics store.

    Usage
    -----
    Writes (from middleware / endpoints):
        asyncio.create_task(collector.record_request(...))
        asyncio.create_task(collector.record_chat(...))
        asyncio.create_task(collector.record_agent(...))
        asyncio.create_task(collector.record_error(...))

    Reads (from /api/analytics endpoints):
        await collector.summary()
        await collector.chat_stats()
        ...
    """

    def __init__(self):
        self._ready    = False
        self._db_path  = str(_DB_PATH)

    async def _init(self) -> None:
        if self._ready:
            return
        aiosqlite = _ensure_aiosqlite()
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_CREATE_SQL)
            await self._migrate_chat_events_columns(db)
            await db.commit()
        self._ready = True
        log.info("[analytics] SQLite ready at %s", self._db_path)

    async def _migrate_chat_events_columns(self, db) -> None:
        """
        [Phase 34] Add prompt_tokens/completion_tokens columns to chat_events
        for databases created before this migration. CREATE TABLE IF NOT
        EXISTS is a no-op on tables that already exist, so older DBs need an
        explicit ALTER TABLE here. Cheap PRAGMA check -- safe to run on every
        startup. Existing rows get the column DEFAULT (0).
        """
        cur = await db.execute("PRAGMA table_info(chat_events)")
        cols = {row[1] for row in await cur.fetchall()}
        if "prompt_tokens" not in cols:
            await db.execute(
                "ALTER TABLE chat_events ADD COLUMN prompt_tokens INTEGER DEFAULT 0"
            )
            log.info("[analytics] Migrated chat_events: added prompt_tokens column")
        if "completion_tokens" not in cols:
            await db.execute(
                "ALTER TABLE chat_events ADD COLUMN completion_tokens INTEGER DEFAULT 0"
            )
            log.info("[analytics] Migrated chat_events: added completion_tokens column")

    async def _exec(self, sql: str, params: tuple = ()) -> None:
        """Fire-and-forget INSERT."""
        aiosqlite = _ensure_aiosqlite()
        try:
            if not self._ready:
                await self._init()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(sql, params)
                await db.commit()
        except Exception as e:
            log.debug("[analytics] write error: %s", e)

    async def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Blocking SELECT — returns list of row dicts."""
        aiosqlite = _ensure_aiosqlite()
        if not self._ready:
            await self._init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Write methods
    # -----------------------------------------------------------------------

    async def record_request(
        self,
        method:      str,
        endpoint:    str,
        status_code: int,
        latency_ms:  float,
    ) -> None:
        await self._exec(
            "INSERT INTO requests (ts, method, endpoint, status_code, latency_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), method, _norm(endpoint), status_code, round(latency_ms, 1)),
        )
        # Auto-record errors
        if status_code >= 500:
            await self._exec(
                "INSERT INTO error_events (ts, endpoint, error_type, message) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), _norm(endpoint), f"HTTP_{status_code}", ""),
            )

    async def record_chat(
        self,
        session_id: Optional[str],
        intent:     str,
        model:      str,
        persona:    str     = "jarvis",
        latency_ms: float   = 0.0,
        tokens:     int     = 0,
        success:    bool    = True,
        prompt_tokens:     int = 0,
        completion_tokens: int = 0,
    ) -> None:
        await self._exec(
            "INSERT INTO chat_events "
            "(ts, session_id, intent, model, persona, latency_ms, tokens, success, "
            "prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), session_id, intent, model, persona,
             round(latency_ms, 1), tokens, 1 if success else 0,
             prompt_tokens, completion_tokens),
        )

    async def record_agent(
        self,
        agent_name: str,
        latency_ms: float = 0.0,
        success:    bool  = True,
    ) -> None:
        await self._exec(
            "INSERT INTO agent_events (ts, agent_name, latency_ms, success) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), agent_name, round(latency_ms, 1), 1 if success else 0),
        )

    async def record_error(
        self,
        endpoint:   str = "",
        error_type: str = "",
        message:    str = "",
        model:      str = "",
    ) -> None:
        await self._exec(
            "INSERT INTO error_events (ts, endpoint, error_type, message, model) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), endpoint, error_type, message[:500], model),
        )

    # -----------------------------------------------------------------------
    # Read methods
    # -----------------------------------------------------------------------

    async def summary(self, days: int = 7) -> dict:
        """High-level totals for the last N days."""
        since = time.time() - days * 86400
        rows = await self._query(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS errors, "
            "AVG(latency_ms) AS avg_latency "
            "FROM requests WHERE ts > ?",
            (since,),
        )
        req = rows[0] if rows else {}

        chat = (await self._query(
            "SELECT COUNT(*) AS total, AVG(latency_ms) AS avg_latency, "
            "SUM(tokens) AS total_tokens "
            "FROM chat_events WHERE ts > ?",
            (since,),
        ))[0]

        agents = (await self._query(
            "SELECT COUNT(*) AS total FROM agent_events WHERE ts > ?",
            (since,),
        ))[0]

        errors = (await self._query(
            "SELECT COUNT(*) AS total FROM error_events WHERE ts > ?",
            (since,),
        ))[0]

        sessions = (await self._query(
            "SELECT COUNT(DISTINCT session_id) AS total "
            "FROM chat_events WHERE ts > ? AND session_id IS NOT NULL",
            (since,),
        ))[0]

        return {
            "period_days":      days,
            "requests": {
                "total":        req.get("total", 0) or 0,
                "success":      req.get("success", 0) or 0,
                "errors":       req.get("errors", 0) or 0,
                "avg_latency_ms": round(req.get("avg_latency") or 0, 1),
            },
            "chat": {
                "total":        chat.get("total", 0) or 0,
                "avg_latency_ms": round(chat.get("avg_latency") or 0, 1),
                "total_tokens": chat.get("total_tokens", 0) or 0,
            },
            "agents": {
                "total_calls":  agents.get("total", 0) or 0,
            },
            "unique_sessions":  sessions.get("total", 0) or 0,
            "errors_total":     errors.get("total", 0) or 0,
        }

    async def chat_stats(self, days: int = 7) -> dict:
        """Chat breakdown by intent and by model."""
        since = time.time() - days * 86400

        by_intent = await self._query(
            "SELECT intent, COUNT(*) AS count, "
            "AVG(latency_ms) AS avg_latency, "
            "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes "
            "FROM chat_events WHERE ts > ? "
            "GROUP BY intent ORDER BY count DESC",
            (since,),
        )

        by_model = await self._query(
            "SELECT model, COUNT(*) AS count, "
            "AVG(latency_ms) AS avg_latency, "
            "SUM(tokens) AS total_tokens, "
            "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes "
            "FROM chat_events WHERE ts > ? "
            "GROUP BY model ORDER BY count DESC",
            (since,),
        )

        by_persona = await self._query(
            "SELECT persona, COUNT(*) AS count "
            "FROM chat_events WHERE ts > ? "
            "GROUP BY persona ORDER BY count DESC",
            (since,),
        )

        return {
            "period_days": days,
            "by_intent":   [
                {**r,
                 "avg_latency_ms":  round(r.get("avg_latency") or 0, 1),
                 "success_rate":    round(r["successes"] / r["count"] * 100, 1) if r["count"] else 0}
                for r in by_intent
            ],
            "by_model": [
                {**r,
                 "avg_latency_ms": round(r.get("avg_latency") or 0, 1),
                 "success_rate":   round(r["successes"] / r["count"] * 100, 1) if r["count"] else 0}
                for r in by_model
            ],
            "by_persona": by_persona,
        }

    async def agent_stats(self, days: int = 7) -> dict:
        """Per-agent call counts and latency."""
        since = time.time() - days * 86400
        rows = await self._query(
            "SELECT agent_name, COUNT(*) AS count, "
            "AVG(latency_ms) AS avg_latency, "
            "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes "
            "FROM agent_events WHERE ts > ? "
            "GROUP BY agent_name ORDER BY count DESC",
            (since,),
        )
        return {
            "period_days": days,
            "agents": [
                {**r,
                 "avg_latency_ms": round(r.get("avg_latency") or 0, 1),
                 "success_rate":   round(r["successes"] / r["count"] * 100, 1) if r["count"] else 0}
                for r in rows
            ],
        }

    async def endpoint_stats(self, limit: int = 20, days: int = 7) -> dict:
        """Top endpoints by call count."""
        since = time.time() - days * 86400
        rows = await self._query(
            "SELECT endpoint, method, COUNT(*) AS count, "
            "AVG(latency_ms) AS avg_latency, "
            "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS successes "
            "FROM requests WHERE ts > ? "
            "GROUP BY endpoint, method ORDER BY count DESC LIMIT ?",
            (since, limit),
        )
        return {
            "period_days": days,
            "endpoints": [
                {**r,
                 "avg_latency_ms": round(r.get("avg_latency") or 0, 1),
                 "success_rate":   round(r["successes"] / r["count"] * 100, 1) if r["count"] else 0}
                for r in rows
            ],
        }

    async def model_stats(self, days: int = 7) -> dict:
        """Persistent model usage stats (survives restarts, unlike LatencyTracker)."""
        since = time.time() - days * 86400
        rows = await self._query(
            "SELECT model, COUNT(*) AS calls, "
            "AVG(latency_ms) AS avg_latency, "
            "MIN(latency_ms) AS min_latency, "
            "MAX(latency_ms) AS max_latency, "
            "SUM(tokens) AS total_tokens, "
            "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes "
            "FROM chat_events WHERE ts > ? "
            "GROUP BY model ORDER BY calls DESC",
            (since,),
        )

        # Enrich with live LatencyTracker p50/p95
        try:
            from src.intelligence.router import get_router
            live = get_router().latency_tracker.all_stats()
        except Exception:
            live = {}

        return {
            "period_days": days,
            "models": [
                {
                    "model":          r["model"],
                    "calls":          r["calls"],
                    "avg_latency_ms": round(r.get("avg_latency") or 0, 1),
                    "min_latency_ms": round(r.get("min_latency") or 0, 1),
                    "max_latency_ms": round(r.get("max_latency") or 0, 1),
                    "total_tokens":   r.get("total_tokens") or 0,
                    "success_rate":   round(r["successes"] / r["calls"] * 100, 1) if r["calls"] else 0,
                    "live_p50_ms":    (live.get(r["model"]) or {}).get("p50"),
                    "live_p95_ms":    (live.get(r["model"]) or {}).get("p95"),
                }
                for r in rows
            ],
        }

    async def get_cost_summary(self, period: str = "7d") -> dict:
        """
        [Phase 34] Aggregate chat_events into a cost & usage summary for the
        requested period (e.g. "24h", "7d", "30d").

        Costs are ESTIMATED from config/pricing.json -- treat as an
        approximation, not a billing record. Models with no pricing entry
        are tracked under `unpriced_models` (token counts retained, cost
        contribution $0). `pricing_last_updated` is surfaced so the caller
        knows how stale the rates might be.

        Uses a single indexed SQL aggregation (GROUP BY model, intent over
        the ts-indexed chat_events table) rather than looping over raw rows
        in Python, so this stays fast even with a large history.

        `cache_hit_rate` is reserved for Phase 22 (Local Intent Cache) and
        is `None` until that phase populates it. `circuit_breaker_events`
        is sourced from Phase 28's diagnostics log when available, else [].
        """
        days  = _parse_period_days(period)
        since = time.time() - days * 86400
        pricing = _load_pricing()

        rows = await self._query(
            "SELECT model, intent, COUNT(*) AS calls, "
            "SUM(tokens) AS total_tokens, "
            "SUM(prompt_tokens) AS total_prompt_tokens, "
            "SUM(completion_tokens) AS total_completion_tokens "
            "FROM chat_events WHERE ts > ? "
            "GROUP BY model, intent ORDER BY calls DESC",
            (since,),
        )

        total_cost:   float = 0.0
        total_tokens: int   = 0
        total_calls:  int   = 0
        by_provider: dict[str, dict] = {}
        by_model:    dict[str, dict] = {}
        by_intent:   dict[str, dict] = {}
        unpriced_models: set[str] = set()

        for r in rows:
            model  = r["model"]  or "unknown"
            intent = r["intent"] or "unknown"
            calls  = r["calls"] or 0
            tokens = r["total_tokens"] or 0
            prompt_tok     = r["total_prompt_tokens"] or 0
            completion_tok = r["total_completion_tokens"] or 0

            # Rows recorded before Phase 34 have prompt/completion == 0;
            # approximate with a 50/50 split of `tokens` so historical data
            # still contributes a (rough) cost instead of $0.
            if prompt_tok == 0 and completion_tok == 0 and tokens:
                prompt_tok     = tokens // 2
                completion_tok = tokens - prompt_tok

            rate = _price_for_model(model, pricing)
            cost = round(
                (prompt_tok / 1000.0) * rate["input"]
                + (completion_tok / 1000.0) * rate["output"],
                6,
            )

            if not rate["priced"]:
                unpriced_models.add(model)

            provider = model.split("/", 1)[0] if "/" in model else model
            prov = by_provider.setdefault(provider, {
                "cost_usd": 0.0, "calls": 0, "tokens": 0, "free_tier": True,
            })
            prov["cost_usd"]  = round(prov["cost_usd"] + cost, 6)
            prov["calls"]    += calls
            prov["tokens"]   += tokens
            prov["free_tier"] = prov["free_tier"] and rate["free_tier"]

            m = by_model.setdefault(model, {
                "cost_usd": 0.0, "calls": 0, "tokens": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "free_tier": rate["free_tier"], "priced": rate["priced"],
            })
            m["cost_usd"]           = round(m["cost_usd"] + cost, 6)
            m["calls"]             += calls
            m["tokens"]            += tokens
            m["prompt_tokens"]     += prompt_tok
            m["completion_tokens"] += completion_tok

            it = by_intent.setdefault(intent, {"cost_usd": 0.0, "calls": 0, "tokens": 0})
            it["cost_usd"] = round(it["cost_usd"] + cost, 6)
            it["calls"]   += calls
            it["tokens"]  += tokens

            total_cost   += cost
            total_tokens += tokens
            total_calls  += calls

        return {
            "period":               period,
            "period_days":          days,
            "total_cost_usd":       round(total_cost, 6),
            "total_tokens":         total_tokens,
            "total_calls":          total_calls,
            "by_provider":          by_provider,
            "by_model":             by_model,
            "by_intent":            by_intent,
            "unpriced_models":      sorted(unpriced_models),
            "pricing_last_updated": pricing.get("_meta", {}).get("pricing_last_updated", "unknown"),
            "cache_hit_rate":       None,  # [Phase 22] populated once intent cache lands
            "circuit_breaker_events": await _circuit_breaker_events_since(since),  # [Phase 28]
        }

    async def error_log(self, limit: int = 50) -> dict:
        """Most recent error events."""
        rows = await self._query(
            "SELECT ts, endpoint, error_type, message, model "
            "FROM error_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return {
            "errors": [
                {**r, "ts_iso": _ts_iso(r["ts"])}
                for r in rows
            ],
            "count": len(rows),
        }

    async def timeline(self, hours: int = 24) -> dict:
        """
        Requests per hour for the last N hours.
        Returns chart-ready data: [{hour: "2026-06-08T14", count: 42, errors: 3}]
        """
        since = time.time() - hours * 3600
        rows  = await self._query(
            "SELECT CAST((ts - ?) / 3600 AS INTEGER) AS hour_bucket, "
            "COUNT(*) AS count, "
            "SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS errors "
            "FROM requests WHERE ts > ? "
            "GROUP BY hour_bucket ORDER BY hour_bucket",
            (since, since),
        )

        # Fill gaps so every hour appears
        import datetime
        now      = datetime.datetime.utcnow()
        buckets  = {r["hour_bucket"]: r for r in rows}
        timeline = []
        for h in range(hours):
            dt    = now - datetime.timedelta(hours=hours - 1 - h)
            label = dt.strftime("%Y-%m-%dT%H")
            bucket = buckets.get(h, {})
            timeline.append({
                "hour":   label,
                "count":  bucket.get("count",  0),
                "errors": bucket.get("errors", 0),
            })

        return {"hours": hours, "timeline": timeline}

    async def reset(self, confirm: bool = False) -> dict:
        """Delete all analytics data. Irreversible."""
        if not confirm:
            return {"error": "Set confirm=True to wipe all analytics data"}
        aiosqlite = _ensure_aiosqlite()
        async with aiosqlite.connect(self._db_path) as db:
            for table in ("requests", "chat_events", "agent_events", "error_events"):
                await db.execute(f"DELETE FROM {table}")
            await db.commit()
        log.warning("[analytics] All analytics data wiped")
        return {"reset": True, "tables_cleared": 4}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_iso(ts: float) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# [Phase 34] Cost & Usage helpers
# ---------------------------------------------------------------------------

def _load_pricing() -> dict:
    """
    Load config/pricing.json, cached after the first read. Returns a safe
    empty structure (everything unpriced/$0) if the file is missing or
    invalid so the cost dashboard degrades gracefully instead of crashing.
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    try:
        with open(_PRICING_PATH, "r", encoding="utf-8") as f:
            _pricing_cache = json.load(f)
    except Exception as e:
        log.debug(f"[analytics] pricing.json load failed: {e}")
        _pricing_cache = {
            "_meta": {"pricing_last_updated": "unknown"},
            "free_suffix_markers": [],
            "provider_defaults": {},
            "model_overrides": {},
        }
    return _pricing_cache


def _price_for_model(model: str, pricing: dict) -> dict:
    """
    Resolve {input, output, free_tier, priced} per-1K-token rates for a
    model id such as "groq/llama-3.1-8b-instant" or
    "openrouter/some-model:free".

    Resolution order:
      1. Exact match in model_overrides (free_tier inferred True if both
         rates are 0 and the entry doesn't say otherwise).
      2. Suffix match against free_suffix_markers (e.g. ":free") -> $0.
      3. provider_defaults[provider] (provider = text before the first "/").
      4. Unknown provider -> $0 with priced=False ("unpriced").
    """
    overrides = pricing.get("model_overrides", {})
    if model in overrides:
        rate = overrides[model]
        input_rate  = rate.get("input", 0.0)
        output_rate = rate.get("output", 0.0)
        free = bool(rate.get("free_tier", input_rate == 0.0 and output_rate == 0.0))
        return {"input": input_rate, "output": output_rate, "free_tier": free, "priced": True}

    for marker in pricing.get("free_suffix_markers", []):
        if marker and model.endswith(marker):
            return {"input": 0.0, "output": 0.0, "free_tier": True, "priced": True}

    provider = model.split("/", 1)[0] if "/" in model else model
    defaults = pricing.get("provider_defaults", {})
    if provider in defaults:
        rate = defaults[provider]
        return {
            "input":     rate.get("input", 0.0),
            "output":    rate.get("output", 0.0),
            "free_tier": bool(rate.get("free_tier", False)),
            "priced":    True,
        }

    return {"input": 0.0, "output": 0.0, "free_tier": False, "priced": False}


def _parse_period_days(period: str) -> float:
    """
    Parse a period string like "7d", "24h", "30d" into a number of days
    (float). Falls back to 7 days for unrecognised/empty input.
    """
    if not period:
        return 7.0
    p = period.strip().lower()
    try:
        if p.endswith("d"):
            return max(float(p[:-1]), 0.0)
        if p.endswith("h"):
            return max(float(p[:-1]) / 24.0, 0.0)
        return max(float(p), 0.0)
    except ValueError:
        return 7.0


async def _circuit_breaker_events_since(since: float, limit: int = 20) -> list[dict]:
    """
    Pull circuit-breaker trip events from Phase 28's diagnostics ring buffer
    (memory/cache/diagnostics.jsonl) for the requested window. Returns []
    gracefully if Phase 28's self-healing module isn't importable/available
    -- this field is purely additive.
    """
    try:
        from src.system.self_healing import get_diagnostics_log
    except Exception:
        return []
    try:
        log_data = await get_diagnostics_log(limit=_MAX_DIAG_SCAN)
    except Exception:
        return []

    events = []
    for entry in log_data.get("entries", []):
        ts = entry.get("ts", 0)
        if ts < since:
            continue
        tripped = entry.get("router_health", {}).get("tripped_models", [])
        if tripped:
            events.append({
                "ts":             ts,
                "ts_iso":         _ts_iso(ts),
                "tripped_models": tripped,
                "actions":        entry.get("actions", []),
            })
    return events[-limit:]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_collector: Optional[AnalyticsCollector] = None


def get_collector() -> AnalyticsCollector:
    global _collector
    if _collector is None:
        _collector = AnalyticsCollector()
    return _collector

