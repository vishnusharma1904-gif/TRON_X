"""
Phase 22 verification: Local Intent Cache & Semantic Command Routing.

Standalone script (no pytest dependency assumed) -- run from the repo root:
    python3 tests/test_phase22_intent_cache.py

Exercises:
  - IntentCache: cosine-similarity lookup/store, INTENT_CACHE_SIM_THRESHOLD,
    SAFE_CACHEABLE_INTENTS whitelist, TTL eviction, clear()       (intent_cache.py)
  - IoT entity-recheck: a cached device action is only served if its
    entity_id is still a known device in nl_mapper._DEVICE_ALIASES (intent_cache.py)
  - parse_clear_cache_command / handle_clear_cache "clear command cache"
    command, wired into commands.py::try_handle_command           (intent_cache.py, commands.py)
  - IntentClassifier.classify(): a fresh classification gets stored, and a
    paraphrase then returns ("iot", sim, "cache_semantic") without any
    keyword/LLM re-classification                                  (intent.py)
  - nl_mapper.nl_to_ha_command(): a cache hit (with valid entity) is served
    with method="cache", bypassing the LLM fallback                (nl_mapper.py)
  - config.py Phase 22 settings + INTENT_CACHE_ENABLED=false no-op
  - real sentence-transformers paraphrase similarity >= 0.98 (SKIP if
    sentence-transformers is not installed)
"""
from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # so relative paths (memory/cache/...) resolve

# chromadb is a heavy optional dependency (not installed in this sandbox).
# Nothing here exercises it directly, but commands.py / intent.py sit in an
# import chain that other phases stub defensively -- do the same for safety.
if "chromadb" not in sys.modules:
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.core.config import get_settings  # noqa: E402
import src.intelligence.intent as intent_mod  # noqa: E402
import src.intelligence.intent_cache as intent_cache_mod  # noqa: E402
from src.intelligence.intent_cache import (  # noqa: E402
    MIN_CONFIDENCE_TO_STORE,
    SAFE_CACHEABLE_INTENTS,
    IntentCache,
    parse_clear_cache_command,
)
from src.intelligence.commands import try_handle_command  # noqa: E402
import src.iot.nl_mapper as nl_mapper_mod  # noqa: E402
from src.iot.nl_mapper import _DEVICE_ALIASES, parse_command  # noqa: E402

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
# Stub embedding function -- a small lookup table of hand-picked vectors so
# similarity-threshold / TTL / whitelist / entity-recheck logic can be tested
# deterministically without sentence-transformers (not installed in this
# sandbox -- see the real-embedding check at the end, which SKIPs).
# =============================================================================
_VEC: dict[str, list[float]] = {
    "turn on the living room light":        [1.0, 0.0, 0.0, 0.0],
    "switch on the living room light":      [0.995, 0.0998, 0.0, 0.0],   # cos ~0.995 vs above
    "turn off the bedroom light":           [0.0, 1.0, 0.0, 0.0],        # unrelated
    "what time is it":                      [0.0, 0.0, 1.0, 0.0],
    "what time is it right now":            [0.0, 0.0, 0.995, 0.0998],   # cos ~0.995 vs above
    "delete all my files":                  [0.0, 0.0, 0.0, 1.0],
    "delete all my files please":           [0.0, 0.0, 0.0, 1.0],
    "turn on the garage light":             [0.6, 0.0, 0.0, 0.8],
    "turn on the garage lighting now":      [0.6, 0.0, 0.0, 0.7997],     # cos ~1.0 vs above
    "please dim the den lights a bit":      [1.0, 0.0, 0.0, 0.0],
    "could you dim the den lights slightly": [0.995, 0.0998, 0.0, 0.0],  # cos ~0.995 vs above
}


def _stub_embed(texts: list[str]) -> list[list[float]]:
    return [_VEC[t] for t in texts]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="intent_cache_test_")
    os.close(fd)
    os.remove(path)  # IntentCache creates it fresh on first use
    return path


print("== Stub vector sanity check ==")
sim_lights = _cos(_VEC["turn on the living room light"], _VEC["switch on the living room light"])
sim_time = _cos(_VEC["what time is it"], _VEC["what time is it right now"])
sim_garage = _cos(_VEC["turn on the garage light"], _VEC["turn on the garage lighting now"])
sim_unrelated = _cos(_VEC["turn on the living room light"], _VEC["turn off the bedroom light"])
check("paraphrase 'lights' sim >= 0.98", sim_lights >= 0.98, detail=str(sim_lights))
check("paraphrase 'time' sim >= 0.98", sim_time >= 0.98, detail=str(sim_time))
check("paraphrase 'garage' sim >= 0.98", sim_garage >= 0.98, detail=str(sim_garage))
check("unrelated phrases sim < 0.98", sim_unrelated < 0.98, detail=str(sim_unrelated))


# =============================================================================
# 1. Basic store/lookup + similarity threshold
# =============================================================================
print("\n== IntentCache: store/lookup + similarity threshold ==")

db1 = _tmp_db()
cache1 = IntentCache(db_path=db1, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)

hit = asyncio.run(cache1.lookup("switch on the living room light"))
check("lookup on empty cache -> None", hit is None, detail=str(hit))

living_room_action = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room"}
check("light.living_room is a known device", living_room_action["entity_id"] in _DEVICE_ALIASES.values())
asyncio.run(cache1.store("turn on the living room light", "iot", living_room_action))

hit = asyncio.run(cache1.lookup("switch on the living room light"))
check("paraphrase hits cache", hit is not None and hit.intent == "iot", detail=str(hit))
check("cached resolved_action carried through",
      hit.resolved_action == living_room_action if hit else False, detail=str(hit))
check("similarity >= threshold (0.98)", hit.similarity >= 0.98 if hit else False, detail=str(hit))

hit2 = asyncio.run(cache1.lookup("turn off the bedroom light"))
check("unrelated phrase -> cache miss", hit2 is None, detail=str(hit2))


# =============================================================================
# 2. Whitelist (SAFE_CACHEABLE_INTENTS) is enforced by store()
# =============================================================================
print("\n== IntentCache: whitelist enforcement ==")

check("'iot' in SAFE_CACHEABLE_INTENTS", "iot" in SAFE_CACHEABLE_INTENTS)
check("'chat' in SAFE_CACHEABLE_INTENTS", "chat" in SAFE_CACHEABLE_INTENTS)
check("'system' NOT in SAFE_CACHEABLE_INTENTS", "system" not in SAFE_CACHEABLE_INTENTS)

before_count = len(cache1._entries)
asyncio.run(cache1.store("delete all my files", "system", {"action": "rm -rf"}))
after_count = len(cache1._entries)
check("store() is a no-op for non-whitelisted intent ('system')",
      after_count == before_count, detail=f"{before_count}->{after_count}")

hit3 = asyncio.run(cache1.lookup("delete all my files please"))
check("destructive phrase never served from cache", hit3 is None, detail=str(hit3))


# =============================================================================
# 3. "chat" intent caching (e.g. "what time is it")
# =============================================================================
print("\n== IntentCache: 'chat' intent (e.g. time/weather queries) ==")

asyncio.run(cache1.store("what time is it", "chat", None))
hit4 = asyncio.run(cache1.lookup("what time is it right now"))
check("chat paraphrase hits cache", hit4 is not None and hit4.intent == "chat", detail=str(hit4))
check("chat resolved_action defaults to {}", hit4.resolved_action == {} if hit4 else False, detail=str(hit4))


# =============================================================================
# 4. IoT entity-recheck edge case
# =============================================================================
print("\n== IntentCache: IoT entity-recheck ==")

db2 = _tmp_db()
cache2 = IntentCache(db_path=db2, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)

bogus_action = {"domain": "light", "service": "turn_on", "entity_id": "light.nonexistent_room"}
check("light.nonexistent_room is NOT a known device",
      bogus_action["entity_id"] not in _DEVICE_ALIASES.values())
asyncio.run(cache2.store("turn on the garage light", "iot", bogus_action))
hit5 = asyncio.run(cache2.lookup("turn on the garage lighting now"))
check("cached entity no longer valid -> miss despite high similarity", hit5 is None, detail=str(hit5))


# =============================================================================
# 5. TTL eviction
# =============================================================================
print("\n== IntentCache: TTL eviction ==")

db3 = _tmp_db()
cache3 = IntentCache(db_path=db3, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)
asyncio.run(cache3.store("turn on the living room light", "iot", living_room_action))

old_ts = time.time() - 31 * 86400
with sqlite3.connect(db3) as conn:
    conn.execute("UPDATE intent_cache SET created_at = ?", (old_ts,))
    conn.commit()
cache3._load_all()

hit6 = asyncio.run(cache3.lookup("switch on the living room light"))
check("expired entry -> cache miss", hit6 is None, detail=str(hit6))

removed = asyncio.run(cache3.evict_expired())
check("evict_expired() removes the stale row", removed == 1, detail=str(removed))
with sqlite3.connect(db3) as conn:
    n = conn.execute("SELECT COUNT(*) FROM intent_cache").fetchone()[0]
check("table empty after eviction", n == 0, detail=str(n))


# =============================================================================
# 6. clear() + "clear command cache" / "reset routines" parser
# =============================================================================
print("\n== IntentCache: clear() + 'clear command cache' parser ==")

db4 = _tmp_db()
cache4 = IntentCache(db_path=db4, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)
asyncio.run(cache4.store("turn on the living room light", "iot", living_room_action))
asyncio.run(cache4.store("what time is it", "chat", None))

check("'clear command cache' matches", parse_clear_cache_command("clear command cache"))
check("'reset routines' matches", parse_clear_cache_command("reset routines"))
check("'Clear my command cache.' matches", parse_clear_cache_command("Clear my command cache."))
check("'flush the intent cache' matches", parse_clear_cache_command("flush the intent cache"))
check("'clear the table' does NOT match", not parse_clear_cache_command("clear the table"))
check("'forget my wifi password' does NOT match", not parse_clear_cache_command("forget my wifi password"))
check("'remember to clear cache later' does NOT match",
      not parse_clear_cache_command("remember to clear cache later"))

n_removed = asyncio.run(cache4.clear())
check("clear() removes both entries", n_removed == 2, detail=str(n_removed))
hit7 = asyncio.run(cache4.lookup("switch on the living room light"))
check("lookup after clear() -> None", hit7 is None, detail=str(hit7))


# =============================================================================
# 7. INTENT_CACHE_ENABLED=false -> instant no-op
# =============================================================================
print("\n== IntentCache: disabled -> no-op ==")

db5 = _tmp_db()
cache5 = IntentCache(db_path=db5, embed_fn=_stub_embed, enabled=False)
asyncio.run(cache5.store("turn on the living room light", "iot", living_room_action))
hit8 = asyncio.run(cache5.lookup("switch on the living room light"))
check("disabled cache: lookup() -> None", hit8 is None, detail=str(hit8))
check("disabled cache: evict_expired() -> 0", asyncio.run(cache5.evict_expired()) == 0)
check("disabled cache: clear() -> 0", asyncio.run(cache5.clear()) == 0)
check("disabled cache: no db file created", not os.path.exists(db5))


# =============================================================================
# 8. IntentClassifier.classify(): semantic cache integration
# =============================================================================
print("\n== IntentClassifier.classify(): semantic cache integration ==")

db6 = _tmp_db()
cache6 = IntentCache(db_path=db6, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)
_orig_get_intent_cache = intent_mod.get_intent_cache
intent_mod.get_intent_cache = lambda: cache6

try:
    clf = intent_mod.IntentClassifier(router=None)

    # First call: semantic cache empty -> falls through to keyword classification
    intent1, conf1, method1 = asyncio.run(clf.classify("turn on the living room light"))
    check("first call classifies as 'iot'", intent1 == "iot", detail=f"{intent1},{conf1},{method1}")
    check("first call NOT served from semantic cache", method1 != "cache_semantic", detail=method1)
    check("first call confidence >= MIN_CONFIDENCE_TO_STORE",
          conf1 >= MIN_CONFIDENCE_TO_STORE, detail=str(conf1))

    # classify() should have stored the result (whitelisted + confident enough)
    stored_hit = asyncio.run(cache6.lookup("switch on the living room light"))
    check("classify() stored the result for future paraphrase lookups",
          stored_hit is not None and stored_hit.intent == "iot", detail=str(stored_hit))
    check("stored resolved_action carries entity 'light.living_room'",
          stored_hit.resolved_action.get("entity_id") == "light.living_room" if stored_hit else False,
          detail=str(stored_hit))

    # Second call with a paraphrase -> served from semantic cache
    intent2, conf2, method2 = asyncio.run(clf.classify("switch on the living room light"))
    check("paraphrase classified as 'iot'", intent2 == "iot", detail=f"{intent2},{conf2},{method2}")
    check("paraphrase served via cache_semantic", method2 == "cache_semantic", detail=method2)
    check("cache hit similarity >= 0.98", conf2 >= 0.98, detail=str(conf2))
finally:
    intent_mod.get_intent_cache = _orig_get_intent_cache


# =============================================================================
# 9. config.py settings
# =============================================================================
print("\n== config.py: Phase 22 settings ==")

settings = get_settings()
check("intent_cache_enabled default True",
      settings.intent_cache_enabled is True, detail=str(settings.intent_cache_enabled))
check("intent_cache_sim_threshold default 0.98",
      settings.intent_cache_sim_threshold == 0.98, detail=str(settings.intent_cache_sim_threshold))
check("intent_cache_ttl_days default 30",
      settings.intent_cache_ttl_days == 30, detail=str(settings.intent_cache_ttl_days))


# =============================================================================
# 10. nl_mapper.nl_to_ha_command(): cache hit bypasses LLM fallback
# =============================================================================
print("\n== nl_mapper.nl_to_ha_command(): cache dispatch ==")

db7 = _tmp_db()
cache7 = IntentCache(db_path=db7, embed_fn=_stub_embed, enabled=True, threshold=0.98, ttl_days=30)
bedroom_action = {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"}
asyncio.run(cache7.store("please dim the den lights a bit", "iot", bedroom_action))

_orig_get_intent_cache2 = intent_cache_mod.get_intent_cache
intent_cache_mod.get_intent_cache = lambda: cache7
try:
    fastpath = parse_command("could you dim the den lights slightly")
    check("fast-path does NOT resolve this phrasing (so cache path is exercised)",
          fastpath is None, detail=str(fastpath))

    result = asyncio.run(nl_mapper_mod.nl_to_ha_command("could you dim the den lights slightly"))
    check("nl_to_ha_command served from cache", result.get("method") == "cache", detail=str(result))
    check("cached entity_id returned", result.get("entity_id") == "light.bedroom", detail=str(result))
finally:
    intent_cache_mod.get_intent_cache = _orig_get_intent_cache2


# =============================================================================
# 11. "clear command cache" wired into commands.py::try_handle_command
# =============================================================================
print("\n== commands.py: 'clear command cache' end-to-end ==")

reply = asyncio.run(try_handle_command("clear command cache", persona="jarvis"))
check("try_handle_command handles 'clear command cache'",
      reply is not None and "cach" in reply.lower(), detail=str(reply))


# =============================================================================
# 12. Real sentence-transformers paraphrase similarity (SKIP if unavailable)
# =============================================================================
print("\n== Real embeddings (optional) ==")

try:
    from src.memory.embeddings import EMBED_DIM, MODEL_ID
    check("embeddings module exposes MODEL_ID", isinstance(MODEL_ID, str) and len(MODEL_ID) > 0, detail=MODEL_ID)
    check("embeddings module exposes EMBED_DIM=384", EMBED_DIM == 384, detail=str(EMBED_DIM))

    from src.memory.embeddings import embed
    vecs = embed(["turn the lights off", "switch off the lights"])
    real_sim = _cos(vecs[0], vecs[1])
    check("real-model paraphrase similarity >= 0.98", real_sim >= 0.98, detail=str(real_sim))
except ModuleNotFoundError as e:
    print(f"  SKIP  sentence-transformers not installed ({e})")


# =============================================================================
# Summary
# =============================================================================
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
