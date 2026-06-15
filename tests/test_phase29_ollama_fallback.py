"""
Phase 29 verification: Local Embedding Offloading & Ollama Mesh Fallback.

Standalone script (no pytest dependency assumed) -- run from the repo root:
    python3 tests/test_phase29_ollama_fallback.py

Exercises:
  - config.py Phase 29 settings (ollama_fallback_enabled,
    ollama_health_check_interval_sec, embedding_backend)
  - _OLLAMA_INTENT_MAP / _OLLAMA_FALLBACK_PRIORITY coverage          (router.py)
  - SmartRouter._check_ollama_health() -- caching + fast failure
    when Ollama is unreachable, True + cache reuse when reachable    (router.py)
  - SmartRouter.complete() -- no regression: AllProvidersExhaustedError
    still raised when Ollama fallback is unreachable/disabled        (router.py)
  - SmartRouter.complete() -- falls back to the category's local Ollama
    model (with disclaimer prepended) once the cloud chain is
    exhausted                                                         (router.py)
  - SmartRouter.complete() -- falls through to the next local model
    if the category's preferred local model errors (e.g. not pulled) (router.py)
  - embeddings.py -- embedding_backend default stays
    "sentence_transformers"; embed() routes to Ollama's
    /api/embeddings when embedding_backend == "ollama"
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # so relative paths (config/models.json, ...) resolve

# chromadb is a heavy optional dependency not installed in this sandbox, and
# is imported transitively via src.memory -- stub it before other imports.
# See test_phase28 for precedent.
if "chromadb" not in sys.modules:
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.core.config import get_settings  # noqa: E402
from src.core.exceptions import AllProvidersExhaustedError  # noqa: E402
from src.intelligence import router as router_mod  # noqa: E402
from src.intelligence.router import (  # noqa: E402
    SmartRouter,
    _OLLAMA_DEFAULT_FALLBACK,
    _OLLAMA_FALLBACK_PRIORITY,
    _OLLAMA_INTENT_MAP,
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


settings = get_settings()


def _make_response(content: str = "hi"):
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(total_tokens=10)
    return SimpleNamespace(choices=[choice], usage=usage)


# =============================================================================
print("=== Config: Phase 29 settings ===")
# =============================================================================
check("ollama_fallback_enabled exists", hasattr(settings, "ollama_fallback_enabled"))
check("ollama_fallback_enabled defaults True", settings.ollama_fallback_enabled is True)
check("ollama_health_check_interval_sec exists", hasattr(settings, "ollama_health_check_interval_sec"))
check("ollama_health_check_interval_sec defaults 60", settings.ollama_health_check_interval_sec == 60)
check("embedding_backend exists", hasattr(settings, "embedding_backend"))
check("embedding_backend defaults sentence_transformers", settings.embedding_backend == "sentence_transformers")


# =============================================================================
print("\n=== _OLLAMA_INTENT_MAP / _OLLAMA_FALLBACK_PRIORITY ===")
# =============================================================================
catalog_categories = set(router_mod._load_catalog()["categories"].keys())
mapped_categories = set(_OLLAMA_INTENT_MAP.keys())
check(
    "every models.json category has an explicit ollama mapping",
    catalog_categories == mapped_categories,
    f"missing={catalog_categories - mapped_categories}, extra={mapped_categories - catalog_categories}",
)
for cat, model in _OLLAMA_INTENT_MAP.items():
    check(f"{cat} -> {model} is an ollama/ model", model.startswith("ollama/"))
    check(f"{cat}'s mapped model is in _OLLAMA_FALLBACK_PRIORITY", model in _OLLAMA_FALLBACK_PRIORITY)
check("_OLLAMA_DEFAULT_FALLBACK is in priority list", _OLLAMA_DEFAULT_FALLBACK in _OLLAMA_FALLBACK_PRIORITY)

ollama_models_in_catalog = {
    "ollama/" + m.split("/", 1)[1]
    for m in router_mod._load_catalog()["provider_configs"]["ollama"]["_models"]
}
check(
    "_OLLAMA_FALLBACK_PRIORITY matches config/models.json's ollama models",
    set(_OLLAMA_FALLBACK_PRIORITY) == ollama_models_in_catalog,
    f"diff={set(_OLLAMA_FALLBACK_PRIORITY) ^ ollama_models_in_catalog}",
)


# =============================================================================
print("\n=== _check_ollama_health() ===")
# =============================================================================
router = SmartRouter()


async def _health_tests() -> None:
    # --- Unreachable -> False, fast (no real network) ---
    with patch("src.intelligence.router.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=ConnectionError("connection refused"))
        instance.aclose = AsyncMock()
        MockClient.return_value = instance

        t0 = time.monotonic()
        result = await router._check_ollama_health()
        elapsed = time.monotonic() - t0

        check("unreachable -> False", result is False)
        check("unreachable check is fast (<2.5s)", elapsed < 2.5, f"{elapsed:.2f}s")
        check("client.aclose() called even on error", instance.aclose.await_count == 1)

    # --- Reset cache, then reachable -> True ---
    router._ollama_health_cache = None
    router._ollama_health_checked = 0.0
    with patch("src.intelligence.router.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        instance.get = AsyncMock(return_value=resp)
        instance.aclose = AsyncMock()
        MockClient.return_value = instance

        result = await router._check_ollama_health()
        check("reachable -> True", result is True)

    # --- Cached -- second call within interval shouldn't re-invoke httpx ---
    with patch("src.intelligence.router.httpx.AsyncClient") as MockClient:
        result = await router._check_ollama_health()
        check("cached result reused (no new httpx.AsyncClient call)", not MockClient.called)
        check("cached result still True", result is True)


asyncio.run(_health_tests())


# =============================================================================
print("\n=== complete(): regression -- exhausted + ollama unreachable/disabled ===")
# =============================================================================
async def _regression_test() -> None:
    # 1) Ollama unreachable -> still raises as before
    r = SmartRouter()
    for m in r._get_chain("fast_chat"):
        for _ in range(5):
            r.health.mark_failure(m)
    r._ollama_health_cache = False
    r._ollama_health_checked = time.monotonic()

    try:
        await r.complete([{"role": "user", "content": "hi"}], category="fast_chat")
        check("raises AllProvidersExhaustedError (ollama unreachable)", False, "did not raise")
    except AllProvidersExhaustedError:
        check("raises AllProvidersExhaustedError (ollama unreachable)", True)

    # 2) Ollama reachable but fallback disabled via settings -> still raises
    r2 = SmartRouter()
    for m in r2._get_chain("fast_chat"):
        for _ in range(5):
            r2.health.mark_failure(m)
    r2._ollama_health_cache = True
    r2._ollama_health_checked = time.monotonic()

    with patch.object(router_mod.settings, "ollama_fallback_enabled", False):
        try:
            await r2.complete([{"role": "user", "content": "hi"}], category="fast_chat")
            check("raises AllProvidersExhaustedError (fallback disabled)", False, "did not raise")
        except AllProvidersExhaustedError:
            check("raises AllProvidersExhaustedError (fallback disabled)", True)


asyncio.run(_regression_test())


# =============================================================================
print("\n=== complete(): falls back to local Ollama when cloud chain exhausted ===")
# =============================================================================
async def _fallback_test() -> None:
    r = SmartRouter()
    for m in r._get_chain("coding"):
        for _ in range(5):
            r.health.mark_failure(m)
    r._ollama_health_cache = True
    r._ollama_health_checked = time.monotonic()

    async def fake_try_model(model_id, messages, stream, kwargs):
        return _make_response("local answer")

    with patch.object(r, "_try_model", side_effect=fake_try_model):
        response, model_id = await r.complete(
            [{"role": "user", "content": "write code"}], category="coding"
        )

    expected = _OLLAMA_INTENT_MAP["coding"]
    check("falls back to category's mapped ollama model", model_id == expected, model_id)
    check(
        "disclaimer prepended",
        response.choices[0].message.content.startswith("(Running on local backup model"),
        response.choices[0].message.content,
    )
    check("original content preserved", "local answer" in response.choices[0].message.content)


asyncio.run(_fallback_test())


# =============================================================================
print("\n=== complete(): falls through to next local model if first errors ===")
# =============================================================================
async def _fallthrough_test() -> None:
    r = SmartRouter()
    for m in r._get_chain("fast_chat"):
        for _ in range(5):
            r.health.mark_failure(m)
    r._ollama_health_cache = True
    r._ollama_health_checked = time.monotonic()

    primary = _OLLAMA_INTENT_MAP["fast_chat"]
    calls: list[str] = []

    async def fake_try_model(model_id, messages, stream, kwargs):
        calls.append(model_id)
        if model_id == primary:
            raise RuntimeError("model not pulled")
        return _make_response("second model answer")

    with patch.object(r, "_try_model", side_effect=fake_try_model):
        response, model_id = await r.complete(
            [{"role": "user", "content": "hi"}], category="fast_chat"
        )

    check("tried primary local model first", calls[0] == primary, calls)
    check(
        "fell through to a different ollama model",
        model_id != primary and model_id.startswith("ollama/"),
        model_id,
    )
    check(
        "disclaimer prepended on fallthrough too",
        response.choices[0].message.content.startswith("(Running on local backup model"),
    )
    # HealthTracker uses a 3-strike circuit breaker (failure_threshold=3), so a
    # single fallthrough failure doesn't trip is_available() to False -- it
    # just records one failure toward that threshold.
    check("primary failure recorded", r.health._failures.get(primary, 0) >= 1)


asyncio.run(_fallthrough_test())


# =============================================================================
print("\n=== embeddings.py: backend selection ===")
# =============================================================================
import importlib  # noqa: E402

emb_mod = importlib.import_module("src.memory.embeddings")
check("embeddings module exposes _embed_ollama", hasattr(emb_mod, "_embed_ollama"))
check("embeddings module default backend is sentence_transformers", emb_mod.settings.embedding_backend == "sentence_transformers")

with patch.object(emb_mod, "settings", SimpleNamespace(
    embedding_backend="ollama",
    ollama_base_url="http://localhost:11434",
    ollama_model="nomic-embed-text",
)):
    with patch("src.memory.embeddings.httpx.Client") as MockClient:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        resp = MagicMock()
        resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
        instance.post.return_value = resp
        MockClient.return_value = instance

        vecs = emb_mod.embed(["hello"])
        check("embed() routes to ollama backend when embedding_backend='ollama'", vecs == [[0.1, 0.2, 0.3]], vecs)
        check("ollama embeddings posted to /api/embeddings", "/api/embeddings" in instance.post.call_args[0][0])


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
