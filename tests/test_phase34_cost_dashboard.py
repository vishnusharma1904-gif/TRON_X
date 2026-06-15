"""
Phase 34 verification: Unified Cost & Usage Dashboard.

Standalone script (no pytest dependency assumed) -- run from the repo root:
    python3 tests/test_phase34_cost_dashboard.py

Exercises:
  - config/pricing.json structure
  - collector._load_pricing() caching + safe fallback on missing file
  - collector._price_for_model() resolution order: model_overrides ->
    free_suffix_markers -> provider_defaults -> unpriced
  - collector._parse_period_days()
  - collector._circuit_breaker_events_since() (Phase 28 integration +
    graceful degradation)
  - AnalyticsCollector._migrate_chat_events_columns() against a
    pre-Phase-34 chat_events table
  - AnalyticsCollector.record_chat() with prompt_tokens/completion_tokens
  - AnalyticsCollector.get_cost_summary() end-to-end: seeded
    multi-provider/model/intent data, GROUP BY aggregation, cost math,
    free-tier handling, unpriced models, period filtering
  - /api/analytics/dashboard route registration
  - orchestrator.py / chat.py wiring for prompt_tokens / completion_tokens
  - panels.js Cost & Usage HUD card wiring + intent-detection regex
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # so relative paths (config/pricing.json, ...) resolve

# chromadb is a heavy optional dependency not installed in this sandbox.
# Stub it before importing anything that transitively imports chroma_db.
if "chromadb" not in sys.modules:
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.analytics import collector as collector_mod  # noqa: E402
from src.analytics.collector import (  # noqa: E402
    AnalyticsCollector,
    _circuit_breaker_events_since,
    _load_pricing,
    _parse_period_days,
    _price_for_model,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# =============================================================================
# 1. config/pricing.json structure
# =============================================================================
print("== config/pricing.json structure ==")

with open("config/pricing.json", encoding="utf-8") as f:
    pricing_file = json.load(f)

check("pricing.json has _meta", "_meta" in pricing_file)
check(
    "pricing.json _meta.pricing_last_updated == 2025-05-01",
    pricing_file["_meta"].get("pricing_last_updated") == "2025-05-01",
    detail=str(pricing_file.get("_meta")),
)
check(
    "pricing.json free_suffix_markers includes ':free'",
    ":free" in pricing_file.get("free_suffix_markers", []),
)
check(
    "pricing.json provider_defaults.groq is free_tier",
    pricing_file["provider_defaults"]["groq"]["free_tier"] is True,
)
check(
    "pricing.json provider_defaults has 12 providers",
    len(pricing_file["provider_defaults"]) == 12,
    detail=str(len(pricing_file["provider_defaults"])),
)
check(
    "pricing.json model_overrides non-empty",
    len(pricing_file.get("model_overrides", {})) > 0,
)


# =============================================================================
# 2. collector._load_pricing()
# =============================================================================
print("\n== collector._load_pricing() ==")

collector_mod._pricing_cache = None
p1 = _load_pricing()
check("_load_pricing returns dict with _meta", "_meta" in p1, detail=str(p1.keys()))
check(
    "_load_pricing pricing_last_updated == 2025-05-01",
    p1["_meta"].get("pricing_last_updated") == "2025-05-01",
)

p2 = _load_pricing()
check("_load_pricing caches result (same object on 2nd call)", p1 is p2)

# Missing-file fallback -- safe empty structure, no crash
orig_pricing_path = collector_mod._PRICING_PATH
try:
    collector_mod._pricing_cache = None
    collector_mod._PRICING_PATH = Path("/nonexistent/pricing.json")
    p3 = _load_pricing()
    check(
        "_load_pricing falls back to safe empty structure on missing file",
        p3["provider_defaults"] == {}
        and p3["model_overrides"] == {}
        and p3["_meta"]["pricing_last_updated"] == "unknown",
        detail=str(p3),
    )
finally:
    collector_mod._PRICING_PATH = orig_pricing_path
    collector_mod._pricing_cache = None  # force a clean real reload below


# =============================================================================
# 3. collector._price_for_model()
# =============================================================================
print("\n== collector._price_for_model() ==")

real_pricing = _load_pricing()
check("real pricing reloaded after fallback test", "groq" in real_pricing.get("provider_defaults", {}))

# -- provider_defaults: free-tier provider (groq) --
rate = _price_for_model("groq/llama-3.1-8b-instant", real_pricing)
check("groq model -> $0 input/output", rate["input"] == 0.0 and rate["output"] == 0.0, detail=str(rate))
check("groq model -> free_tier True", rate["free_tier"] is True, detail=str(rate))
check("groq model -> priced True (explicit $0, not 'unpriced')", rate["priced"] is True, detail=str(rate))

# -- free_suffix_markers (':free') --
rate = _price_for_model("openrouter/some-model:free", real_pricing)
check(
    "':free' suffix -> $0 + free_tier True + priced True",
    rate == {"input": 0.0, "output": 0.0, "free_tier": True, "priced": True},
    detail=str(rate),
)

# -- model_overrides exact match --
rate = _price_for_model("gemini/gemini-1.5-flash", real_pricing)
check("gemini-1.5-flash override input == 0.000075", rate["input"] == 0.000075, detail=str(rate))
check("gemini-1.5-flash override output == 0.0003", rate["output"] == 0.0003, detail=str(rate))
check("gemini-1.5-flash not free_tier (nonzero rates)", rate["free_tier"] is False, detail=str(rate))
check("gemini-1.5-flash priced True", rate["priced"] is True, detail=str(rate))

# -- provider_defaults fallback (model not overridden, provider known) --
rate = _price_for_model("deepseek/deepseek-chat", real_pricing)
check(
    "deepseek/deepseek-chat falls back to provider default rates",
    rate["input"] == 0.00027 and rate["output"] == 0.0011,
    detail=str(rate),
)
check("deepseek provider default -> priced True", rate["priced"] is True, detail=str(rate))
check("deepseek provider default -> not free_tier", rate["free_tier"] is False, detail=str(rate))

# -- unknown provider -> unpriced --
rate = _price_for_model("totally_unknown_provider/some-model", real_pricing)
check(
    "unknown provider -> $0 + free_tier False + priced False",
    rate == {"input": 0.0, "output": 0.0, "free_tier": False, "priced": False},
    detail=str(rate),
)

# -- explicit $0 override (cerebras) -> priced True, not 'unpriced' --
rate = _price_for_model("cerebras/llama-3.1-8b", real_pricing)
check(
    "cerebras override -> priced True + free_tier True (explicit $0)",
    rate["priced"] is True and rate["free_tier"] is True,
    detail=str(rate),
)


# =============================================================================
# 4. collector._parse_period_days()
# =============================================================================
print("\n== collector._parse_period_days() ==")

check("'7d' -> 7.0", _parse_period_days("7d") == 7.0)
check("'24h' -> 1.0", _parse_period_days("24h") == 1.0)
check("'30d' -> 30.0", _parse_period_days("30d") == 30.0)
check("'2.5d' -> 2.5", _parse_period_days("2.5d") == 2.5)
check("'' -> 7.0 (default)", _parse_period_days("") == 7.0)
check("None -> 7.0 (default)", _parse_period_days(None) == 7.0)
check("'garbage' -> 7.0 (fallback)", _parse_period_days("garbage") == 7.0)
check("'0d' -> 0.0", _parse_period_days("0d") == 0.0)
check("'-5d' -> 0.0 (clamped, not negative)", _parse_period_days("-5d") == 0.0)


# =============================================================================
# 5. collector._circuit_breaker_events_since()  (Phase 28 integration)
# =============================================================================
print("\n== collector._circuit_breaker_events_since() ==")

import src.system.self_healing as self_healing_mod  # noqa: E402

now = time.time()
fake_diag_log = {
    "entries": [
        {"ts": now - 100000, "router_health": {"tripped_models": ["old/model"]}, "actions": ["old action"]},  # too old
        {"ts": now - 100, "router_health": {"tripped_models": []}, "actions": []},  # no trips
        {"ts": now - 50, "router_health": {"tripped_models": ["m1", "m2"]}, "actions": ["biased toward m3"]},  # recent trip
    ]
}

orig_get_diag = getattr(self_healing_mod, "get_diagnostics_log", None)


async def fake_get_diagnostics_log(limit=50):
    return fake_diag_log


try:
    self_healing_mod.get_diagnostics_log = fake_get_diagnostics_log
    events = asyncio.run(_circuit_breaker_events_since(now - 1000))
    check(
        "filters out events before `since`",
        all(e["ts"] >= now - 1000 for e in events),
        detail=str(events),
    )
    check("filters out entries with no tripped_models", len(events) == 1, detail=str(events))
    check(
        "returns tripped_models + actions for the recent trip",
        events[0]["tripped_models"] == ["m1", "m2"] and events[0]["actions"] == ["biased toward m3"],
        detail=str(events),
    )
    check("event includes ts_iso", "ts_iso" in events[0], detail=str(events))

    # get_diagnostics_log raising -> graceful []
    async def raising_get_diagnostics_log(limit=50):
        raise RuntimeError("boom")

    self_healing_mod.get_diagnostics_log = raising_get_diagnostics_log
    events2 = asyncio.run(_circuit_breaker_events_since(now - 1000))
    check("get_diagnostics_log error -> [] (no raise)", events2 == [], detail=str(events2))

    # get_diagnostics_log unimportable -> graceful []
    del self_healing_mod.get_diagnostics_log
    events3 = asyncio.run(_circuit_breaker_events_since(now - 1000))
    check("missing get_diagnostics_log attr -> [] (no raise)", events3 == [], detail=str(events3))
finally:
    if orig_get_diag is not None:
        self_healing_mod.get_diagnostics_log = orig_get_diag


# =============================================================================
# 6. AnalyticsCollector: pre-Phase-34 schema migration
# =============================================================================
print("\n== AnalyticsCollector schema migration (chat_events) ==")

aiosqlite = collector_mod._ensure_aiosqlite()

tmpdir = tempfile.mkdtemp()
try:
    old_db = os.path.join(tmpdir, "old_schema.db")

    async def make_old_schema():
        async with aiosqlite.connect(old_db) as db:
            await db.execute(
                """
                CREATE TABLE chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    session_id TEXT,
                    intent TEXT NOT NULL,
                    model TEXT NOT NULL,
                    persona TEXT NOT NULL DEFAULT 'jarvis',
                    latency_ms REAL NOT NULL,
                    tokens INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 1
                )
                """
            )
            await db.execute(
                "INSERT INTO chat_events "
                "(ts, session_id, intent, model, persona, latency_ms, tokens, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time() - 86400, "old-session", "chat", "groq/llama-3.1-8b-instant", "jarvis", 500.0, 1500, 1),
            )
            await db.commit()

    asyncio.run(make_old_schema())

    async def table_cols():
        async with aiosqlite.connect(old_db) as db:
            cur = await db.execute("PRAGMA table_info(chat_events)")
            return {row[1] for row in await cur.fetchall()}

    old_cols = asyncio.run(table_cols())
    check("pre-migration: no prompt_tokens column", "prompt_tokens" not in old_cols, detail=str(old_cols))
    check("pre-migration: no completion_tokens column", "completion_tokens" not in old_cols, detail=str(old_cols))

    collector = AnalyticsCollector()
    collector._db_path = old_db
    collector._ready = False
    asyncio.run(collector._init())

    new_cols = asyncio.run(table_cols())
    check("migration adds prompt_tokens column", "prompt_tokens" in new_cols, detail=str(new_cols))
    check("migration adds completion_tokens column", "completion_tokens" in new_cols, detail=str(new_cols))

    rows = asyncio.run(collector._query("SELECT prompt_tokens, completion_tokens, tokens FROM chat_events"))
    check("pre-existing row backfilled prompt_tokens=0", rows[0]["prompt_tokens"] == 0, detail=str(rows))
    check("pre-existing row backfilled completion_tokens=0", rows[0]["completion_tokens"] == 0, detail=str(rows))
    check("pre-existing row keeps its original tokens value", rows[0]["tokens"] == 1500, detail=str(rows))

    # _init() must be idempotent on an already-migrated DB (no re-ALTER, no crash)
    collector._ready = False
    asyncio.run(collector._init())
    check("_init() idempotent on already-migrated DB", True)
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# 7. AnalyticsCollector.get_cost_summary() -- end-to-end
# =============================================================================
print("\n== AnalyticsCollector.get_cost_summary() end-to-end ==")

tmpdir2 = tempfile.mkdtemp()
try:
    db_path = os.path.join(tmpdir2, "analytics.db")
    collector = AnalyticsCollector()
    collector._db_path = db_path
    collector._ready = False

    # -- Seed data --------------------------------------------------------
    # 2x groq (free tier), intent=chat, prompt=1000/completion=500 each
    for _ in range(2):
        asyncio.run(collector.record_chat(
            session_id="s1", intent="chat", model="groq/llama-3.1-8b-instant",
            persona="jarvis", latency_ms=200, tokens=1500, success=True,
            prompt_tokens=1000, completion_tokens=500,
        ))
    # 1x gemini-1.5-flash, intent=research, prompt=2000/completion=1000
    asyncio.run(collector.record_chat(
        session_id="s2", intent="research", model="gemini/gemini-1.5-flash",
        persona="jarvis", latency_ms=800, tokens=3000, success=True,
        prompt_tokens=2000, completion_tokens=1000,
    ))
    # 1x unknown/unpriced model, intent=coding, old-style record (no token split)
    asyncio.run(collector.record_chat(
        session_id="s3", intent="coding", model="totally_unknown/some-model",
        persona="friday", latency_ms=400, tokens=600, success=True,
        prompt_tokens=0, completion_tokens=0,
    ))
    # 1x record from 29 days ago -- excluded from a 7d window, included in a
    # 30d window (29d comfortably clears both boundaries; the `ts > since`
    # cutoff is a strict inequality computed at call time, so a record dated
    # exactly "30 days ago" can fall on the wrong side by a few microseconds).
    asyncio.run(collector._exec(
        "INSERT INTO chat_events "
        "(ts, session_id, intent, model, persona, latency_ms, tokens, success, prompt_tokens, completion_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time() - 29 * 86400, "s4", "chat", "gemini/gemini-1.5-flash", "jarvis", 300.0, 1000, 1, 600, 400),
    ))

    summary = asyncio.run(collector.get_cost_summary(period="7d"))

    check("period echoed back", summary["period"] == "7d")
    check("period_days == 7.0", summary["period_days"] == 7.0)

    GEMINI_COST = round((2000 / 1000.0) * 0.000075 + (1000 / 1000.0) * 0.0003, 6)
    check(
        "total_cost_usd matches manual calc (only gemini priced)",
        summary["total_cost_usd"] == GEMINI_COST,
        detail=f"got={summary['total_cost_usd']} expected={GEMINI_COST}",
    )
    check(
        "total_tokens excludes the 30-day-old row",
        summary["total_tokens"] == 1500 + 1500 + 3000 + 600,
        detail=str(summary["total_tokens"]),
    )
    check("total_calls == 4 (30-day-old row excluded)", summary["total_calls"] == 4, detail=str(summary["total_calls"]))

    # -- by_provider --
    check(
        "by_provider has groq, gemini, totally_unknown",
        set(summary["by_provider"].keys()) == {"groq", "gemini", "totally_unknown"},
        detail=str(summary["by_provider"]),
    )
    check("groq provider cost == 0", summary["by_provider"]["groq"]["cost_usd"] == 0.0, detail=str(summary["by_provider"]["groq"]))
    check("groq provider free_tier True", summary["by_provider"]["groq"]["free_tier"] is True, detail=str(summary["by_provider"]["groq"]))
    check("groq provider calls == 2", summary["by_provider"]["groq"]["calls"] == 2, detail=str(summary["by_provider"]["groq"]))
    check(
        "gemini provider cost == GEMINI_COST",
        summary["by_provider"]["gemini"]["cost_usd"] == GEMINI_COST,
        detail=str(summary["by_provider"]["gemini"]),
    )
    check("gemini provider free_tier False", summary["by_provider"]["gemini"]["free_tier"] is False, detail=str(summary["by_provider"]["gemini"]))
    check(
        "totally_unknown provider cost == 0 (unpriced)",
        summary["by_provider"]["totally_unknown"]["cost_usd"] == 0.0,
        detail=str(summary["by_provider"]["totally_unknown"]),
    )

    # -- by_model --
    check("by_model has 3 distinct models", len(summary["by_model"]) == 3, detail=str(list(summary["by_model"].keys())))
    groq_model = summary["by_model"]["groq/llama-3.1-8b-instant"]
    check("groq model: 2 calls aggregated via GROUP BY", groq_model["calls"] == 2, detail=str(groq_model))
    check("groq model: prompt_tokens summed == 2000", groq_model["prompt_tokens"] == 2000, detail=str(groq_model))
    check("groq model: completion_tokens summed == 1000", groq_model["completion_tokens"] == 1000, detail=str(groq_model))

    unknown_model = summary["by_model"]["totally_unknown/some-model"]
    check("unpriced model: 50/50 split fallback prompt_tokens==300", unknown_model["prompt_tokens"] == 300, detail=str(unknown_model))
    check("unpriced model: 50/50 split fallback completion_tokens==300", unknown_model["completion_tokens"] == 300, detail=str(unknown_model))
    check("unpriced model: priced == False", unknown_model["priced"] is False, detail=str(unknown_model))

    # -- by_intent --
    check(
        "by_intent has chat/research/coding",
        set(summary["by_intent"].keys()) == {"chat", "research", "coding"},
        detail=str(summary["by_intent"]),
    )
    check("by_intent chat calls == 2", summary["by_intent"]["chat"]["calls"] == 2, detail=str(summary["by_intent"]["chat"]))
    check(
        "by_intent research cost == GEMINI_COST",
        summary["by_intent"]["research"]["cost_usd"] == GEMINI_COST,
        detail=str(summary["by_intent"]["research"]),
    )

    # -- edge-case fields --
    check(
        "unpriced_models lists totally_unknown/some-model",
        summary["unpriced_models"] == ["totally_unknown/some-model"],
        detail=str(summary["unpriced_models"]),
    )
    check("pricing_last_updated == 2025-05-01", summary["pricing_last_updated"] == "2025-05-01", detail=str(summary["pricing_last_updated"]))
    check("cache_hit_rate is None (Phase 22 not yet landed)", summary["cache_hit_rate"] is None)
    check("circuit_breaker_events is a list", isinstance(summary["circuit_breaker_events"], list), detail=str(summary["circuit_breaker_events"]))

    # -- internal consistency: per-group costs sum to the grand total --
    check(
        "by_provider costs sum to total_cost_usd",
        round(sum(p["cost_usd"] for p in summary["by_provider"].values()), 6) == summary["total_cost_usd"],
    )
    check(
        "by_model costs sum to total_cost_usd",
        round(sum(m["cost_usd"] for m in summary["by_model"].values()), 6) == summary["total_cost_usd"],
    )
    check(
        "by_intent costs sum to total_cost_usd",
        round(sum(i["cost_usd"] for i in summary["by_intent"].values()), 6) == summary["total_cost_usd"],
    )

    # -- 30d window includes the older row too --
    summary30 = asyncio.run(collector.get_cost_summary(period="30d"))
    check("30d window includes the 30-day-old row", summary30["total_calls"] == 5, detail=str(summary30["total_calls"]))
    check("30d period_days == 30.0", summary30["period_days"] == 30.0)

    # -- GROUP BY aggregation, not a Python loop over raw rows --
    raw_rows = asyncio.run(collector._query(
        "SELECT model, intent, COUNT(*) AS calls FROM chat_events WHERE ts > ? "
        "GROUP BY model, intent ORDER BY calls DESC",
        (time.time() - 7 * 86400,),
    ))
    check(
        "GROUP BY query returns one row per (model,intent) pair (3), not per chat_event (4)",
        len(raw_rows) == 3,
        detail=str(raw_rows),
    )
finally:
    shutil.rmtree(tmpdir2, ignore_errors=True)


# =============================================================================
# 8. /api/analytics/dashboard route registration
# =============================================================================
print("\n== /api/analytics/dashboard route registration ==")

try:
    from src.api.analytics import router as analytics_router  # noqa: E402

    all_paths = [r.path for r in analytics_router.routes]
    dashboard_routes = [r for r in analytics_router.routes if r.path == "/api/analytics/dashboard"]
    check("/api/analytics/dashboard route registered", len(dashboard_routes) == 1, detail=str(all_paths))
    if dashboard_routes:
        check("/api/analytics/dashboard is GET", "GET" in dashboard_routes[0].methods, detail=str(dashboard_routes[0].methods))
except ModuleNotFoundError as e:
    print(f"  SKIP  /api/analytics/dashboard route registration (fastapi not installed: {e})")


# =============================================================================
# 9. orchestrator.py / chat.py: prompt_tokens / completion_tokens wiring
# =============================================================================
print("\n== orchestrator.py / chat.py token-split wiring (static checks) ==")

orch_src = Path("src/intelligence/orchestrator.py").read_text(encoding="utf-8")
check(
    "orchestrator extracts prompt_tokens from response.usage",
    'getattr(response.usage, "prompt_tokens", 0)' in orch_src,
)
check(
    "orchestrator extracts completion_tokens from response.usage",
    'getattr(response.usage, "completion_tokens", 0)' in orch_src,
)
check(
    "orchestrator main return dict includes prompt_tokens",
    re.search(r'"prompt_tokens":\s*prompt_tokens\s*,', orch_src) is not None,
)
check(
    "orchestrator main return dict includes completion_tokens",
    re.search(r'"completion_tokens":\s*completion_tokens\s*,', orch_src) is not None,
)

chat_src = Path("src/api/chat.py").read_text(encoding="utf-8")
check(
    "_record_chat passes prompt_tokens to record_chat",
    re.search(r'prompt_tokens\s*=\s*int\(result\.get\("prompt_tokens",\s*0\)\)', chat_src) is not None,
)
check(
    "_record_chat passes completion_tokens to record_chat",
    re.search(r'completion_tokens\s*=\s*int\(result\.get\("completion_tokens",\s*0\)\)', chat_src) is not None,
)


# =============================================================================
# 10. panels.js: Cost & Usage HUD card wiring + intent-detection regex
# =============================================================================
print("\n== panels.js Cost & Usage HUD card wiring ==")

panels_src = Path("static/js/panels.js").read_text(encoding="utf-8")
check("panels.js defines renderCostDashboard", "function renderCostDashboard" in panels_src)
check("renderCostDashboard fetches /api/analytics/dashboard", "/api/analytics/dashboard" in panels_src)
check("detectIntent recognizes a 'cost' intent type", "type:'cost'" in panels_src)
check(
    "triggerCard wires 'cost' -> renderCostDashboard",
    "case 'cost':" in panels_src and "renderCostDashboard()" in panels_src,
)

# Ordering: the cost check must run before the generic crypto/system checks
# it could otherwise collide with (e.g. "token usage", "API usage").
cost_idx = panels_src.index("type:'cost'")
check(
    "cost check precedes generic crypto regex (avoids 'token' collision)",
    cost_idx < panels_src.index("crypto|coin|altcoin|blockchain|token|defi|nft"),
)
check(
    "cost check precedes 'system' intent definition (avoids 'usage' collision)",
    cost_idx < panels_src.index("type:'system'"),
)

# Extract and exercise the actual cost-detection regex against sample input.
m = re.search(r"if\(/(.+?)/\.test\(l\)\) return \{type:'cost'\};", panels_src)
check("cost-detection regex extracted from panels.js", m is not None)
if m:
    cost_re = re.compile(m.group(1))

    positive_cases = [
        "what's my api cost this week",
        "how much have i spent on ai this month",
        "show me the cost dashboard",
        "token spending breakdown",
        "billing summary please",
        "what's my budget for openai",
        "show token usage costs",
        "how much money have i spent",
        "what's my api usage cost",
    ]
    for msg in positive_cases:
        check(f"cost regex matches: {msg!r}", cost_re.search(msg) is not None)

    negative_cases = [
        "what's the weather in london",
        "show me cpu usage",
        "bitcoin price today",
        "show me the analytics dashboard",
        "how much ram is my system using",
    ]
    for msg in negative_cases:
        check(f"cost regex does NOT match: {msg!r}", cost_re.search(msg) is None)


# =============================================================================
# Summary
# =============================================================================
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
