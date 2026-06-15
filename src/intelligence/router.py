"""
TRON-X Smart Router  v3
------------------------
104 open-source models across 14 providers.
  * Provider-specific param filtering
  * Sliding-window rate limit tracking per provider
  * Circuit-breaker health tracking per model
  * Automatic failover across the full fallback chain
  * Exponential backoff on transient errors
  * [Phase 3] Latency-aware chain reordering (P50 rolling window)
  * [Phase 3] A/B model testing framework with per-variant metrics
  * [Phase 3] Per-intent preferred-model injection
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import httpx
import litellm
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.config import get_settings
from src.core.exceptions import AllProvidersExhaustedError, ProviderError, RateLimitError
from src.core.logger import log

# Silence LiteLLM noise
litellm.suppress_debug_info = True
litellm.set_verbose = False

settings = get_settings()

# Load model catalog
_CATALOG_PATH = Path("config/models.json")


def _load_catalog() -> dict:
    with open(_CATALOG_PATH, "r") as f:
        return json.load(f)


def _build_safe_params(catalog: dict) -> dict[str, set[str]]:
    return {
        provider: set(cfg["safe_params"])
        for provider, cfg in catalog["provider_configs"].items()
    }


# =============================================================================
# [Phase 29] Local Embedding Offloading & Ollama Mesh Fallback
# =============================================================================
# Per-category preferred local model when ALL cloud providers in a chain are
# exhausted/circuit-broken/rate-limited. Picked from the 7 models declared
# under provider_configs.ollama._models in config/models.json.
_OLLAMA_INTENT_MAP: dict[str, str] = {
    "fast_chat":    "ollama/llama3.2",
    "fast_edge":    "ollama/phi3",
    "reasoning":    "ollama/qwen2.5",
    "math":         "ollama/qwen2.5",
    "coding":       "ollama/deepseek-coder-v2",
    "cad":          "ollama/codellama",
    "creative":     "ollama/mistral",
    "research":     "ollama/llama3.1",
    "academic":     "ollama/qwen2.5",
    "medical":      "ollama/llama3.1",
    "long_context": "ollama/qwen2.5",
    "iot":          "ollama/llama3.2",
    "system":       "ollama/llama3.2",
    "vision":       "ollama/llama3.2",  # no local vision model -- best-effort text fallback
}
_OLLAMA_DEFAULT_FALLBACK = "ollama/llama3.2"

# Tried in order if a category's preferred local model isn't pulled.
_OLLAMA_FALLBACK_PRIORITY: list[str] = [
    "ollama/llama3.2",
    "ollama/qwen2.5",
    "ollama/llama3.1",
    "ollama/mistral",
    "ollama/phi3",
    "ollama/deepseek-coder-v2",
    "ollama/codellama",
]

_LOCAL_FALLBACK_DISCLAIMER = (
    "(Running on local backup model — cloud providers are unavailable) "
)


def _apply_local_fallback_disclaimer(response: Any, stream: bool) -> Any:
    """Prepend a brief disclaimer so the user knows a degraded local Ollama
    model answered instead of the usual cloud chain.

    Non-streaming responses are edited in place. Streaming responses are
    wrapped to inject one synthetic leading chunk; if litellm's chunk shape
    doesn't match what we expect, the stream is returned untouched (no crash,
    just no disclaimer).
    """
    if not stream:
        try:
            msg = response.choices[0].message
            msg.content = _LOCAL_FALLBACK_DISCLAIMER + (msg.content or "")
        except Exception as e:
            log.debug(f"[router] Could not prepend local-fallback disclaimer: {e}")
        return response

    async def _wrapped():
        try:
            yield litellm.ModelResponse(
                choices=[{"index": 0, "delta": {"content": _LOCAL_FALLBACK_DISCLAIMER}}],
                stream=True,
            )
        except Exception as e:
            log.debug(f"[router] Could not build local-fallback disclaimer chunk: {e}")
        async for chunk in response:
            yield chunk

    return _wrapped()


# =============================================================================
# Health Tracker  (circuit breaker per model)
# =============================================================================

class HealthTracker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 120):
        self._failures:   dict[str, int]   = defaultdict(int)
        self._tripped_at: dict[str, float] = {}
        self._threshold  = failure_threshold
        self._cooldown   = cooldown_seconds

    def mark_success(self, model_id: str) -> None:
        self._failures[model_id] = 0
        self._tripped_at.pop(model_id, None)

    def mark_failure(self, model_id: str) -> None:
        self._failures[model_id] += 1
        if self._failures[model_id] >= self._threshold:
            self._tripped_at[model_id] = time.monotonic()
            log.warning(f"[router] Circuit tripped for [bold]{model_id}[/bold] "
                        f"({self._failures[model_id]} failures)")

    def is_available(self, model_id: str) -> bool:
        if model_id not in self._tripped_at:
            return True
        elapsed = time.monotonic() - self._tripped_at[model_id]
        if elapsed >= self._cooldown:
            log.info(f"[router] Cooldown expired for {model_id} — probing")
            del self._tripped_at[model_id]
            self._failures[model_id] = 0
            return True
        return False

    def status(self) -> dict[str, str]:
        result = {}
        for model_id, tripped in self._tripped_at.items():
            remaining = self._cooldown - (time.monotonic() - tripped)
            result[model_id] = f"degraded (cooldown {int(remaining)}s)"
        return result

    def get_status_summary(self) -> dict:
        """
        [Phase 28] Diagnostic summary for self-healing / HUD.
        Returns currently-tripped models (with remaining cooldown) plus
        cumulative failure counts for every model that has failed at least
        once since startup (used to spot flaky-but-not-yet-tripped models).
        """
        tripped_detail = []
        for model_id, tripped_at in self._tripped_at.items():
            remaining = max(0, self._cooldown - (time.monotonic() - tripped_at))
            tripped_detail.append({"model": model_id, "cooldown_remaining_s": int(remaining)})
        trip_counts = {m: c for m, c in self._failures.items() if c > 0}
        return {
            "tripped_models":  [t["model"] for t in tripped_detail],
            "tripped_detail":  tripped_detail,
            "trip_counts":     trip_counts,
            "threshold":       self._threshold,
            "cooldown_seconds": self._cooldown,
        }


# =============================================================================
# Rate Limiter  (sliding window per provider)
# =============================================================================

class SlidingWindowRateLimiter:
    def __init__(self, catalog: dict):
        self._rpm_limits: dict[str, int] = {
            p: cfg.get("rpm_limit", 30)
            for p, cfg in catalog["provider_configs"].items()
        }
        self._windows: dict[str, deque] = defaultdict(deque)

    def _provider_of(self, model_id: str) -> str:
        return model_id.split("/")[0]

    def is_limited(self, model_id: str) -> bool:
        provider = self._provider_of(model_id)
        limit    = self._rpm_limits.get(provider, 30)
        window   = self._windows[provider]
        now      = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        return len(window) >= limit

    def record(self, model_id: str) -> None:
        provider = self._provider_of(model_id)
        self._windows[provider].append(time.monotonic())

    def remaining(self, model_id: str) -> int:
        provider = self._provider_of(model_id)
        limit    = self._rpm_limits.get(provider, 30)
        window   = self._windows[provider]
        now      = time.monotonic()
        active   = sum(1 for t in window if now - t <= 60)
        return max(0, limit - active)


# =============================================================================
# Latency Tracker  (rolling P50/P95 per model)   [Phase 3]
# =============================================================================

class LatencyTracker:
    """Tracks response latency per model in a fixed rolling window."""

    def __init__(self, window: int = 20):
        self._latencies: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def record(self, model_id: str, latency_ms: float) -> None:
        self._latencies[model_id].append(latency_ms)

    def p50(self, model_id: str) -> float | None:
        samples = list(self._latencies[model_id])
        if not samples:
            return None
        s = sorted(samples)
        return s[len(s) // 2]

    def stats(self, model_id: str) -> dict:
        samples = sorted(self._latencies[model_id])
        if not samples:
            return {"p50": None, "p95": None, "mean": None, "n": 0}
        n = len(samples)
        return {
            "p50":  round(samples[n // 2], 1),
            "p95":  round(samples[min(int(n * 0.95), n - 1)], 1),
            "mean": round(sum(samples) / n, 1),
            "n":    n,
        }

    def sort_by_latency(self, model_ids: list[str]) -> list[str]:
        """Sort models by P50; unknowns (no data yet) go last."""
        def _key(m: str) -> float:
            p = self.p50(m)
            return p if p is not None else float("inf")
        return sorted(model_ids, key=_key)

    def all_stats(self) -> dict:
        return {m: self.stats(m) for m in self._latencies}


# =============================================================================
# A/B Test Manager   [Phase 3]
# =============================================================================

class ABTestManager:
    """
    Weighted A/B model experiments with per-variant latency/token metrics.

    Variant spec:  {"model": "provider/model-id", "weight": 0.7}
    traffic_pct:   fraction of requests routed through experiment (0-1).
                   Remaining traffic uses the default chain order.
    """

    def __init__(self):
        self._experiments: dict[str, dict]            = {}
        self._metrics:     dict[str, dict[str, dict]] = {}

    def register(
        self,
        experiment_id: str,
        variants:      list[dict],
        category:      str,
        traffic_pct:   float = 1.0,
    ) -> None:
        self._experiments[experiment_id] = {
            "category":    category,
            "variants":    variants,
            "traffic_pct": traffic_pct,
            "created_at":  time.time(),
        }
        self._metrics[experiment_id] = {
            v["model"]: {
                "calls": 0, "successes": 0,
                "latency_sum": 0.0, "tokens_sum": 0,
            }
            for v in variants
        }
        log.info(
            f"[ab-test] '{experiment_id}' registered — "
            f"{len(variants)} variants, traffic={traffic_pct:.0%}, cat={category}"
        )

    def select(self, experiment_id: str) -> str | None:
        """Pick a variant by weighted random; None = skip experiment this call."""
        exp = self._experiments.get(experiment_id)
        if not exp:
            return None
        if random.random() > exp["traffic_pct"]:
            return None
        variants = exp["variants"]
        total    = sum(v["weight"] for v in variants)
        r        = random.random() * total
        cumsum   = 0.0
        for v in variants:
            cumsum += v["weight"]
            if r <= cumsum:
                return v["model"]
        return variants[-1]["model"]

    def record(
        self,
        experiment_id: str,
        model_id:      str,
        latency_ms:    float,
        tokens:        int,
        success:       bool,
    ) -> None:
        m = self._metrics.get(experiment_id, {}).get(model_id)
        if m is None:
            return
        m["calls"] += 1
        if success:
            m["successes"]   += 1
            m["latency_sum"] += latency_ms
            m["tokens_sum"]  += tokens

    def experiment_for_category(self, category: str) -> str | None:
        for exp_id, cfg in self._experiments.items():
            if cfg["category"] == category:
                return exp_id
        return None

    def results(self) -> dict:
        out: dict = {}
        for exp_id, metrics in self._metrics.items():
            cfg = self._experiments[exp_id]
            out[exp_id] = {
                "category":    cfg["category"],
                "traffic_pct": cfg["traffic_pct"],
                "created_at":  cfg["created_at"],
                "variants": {
                    model: {
                        "calls":          m["calls"],
                        "success_rate":   round(m["successes"] / m["calls"], 3)
                                          if m["calls"] else 0,
                        "avg_latency_ms": round(m["latency_sum"] / m["successes"], 1)
                                          if m["successes"] else None,
                        "avg_tokens":     round(m["tokens_sum"] / m["successes"], 1)
                                          if m["successes"] else None,
                    }
                    for model, m in metrics.items()
                },
            }
        return out


# Categories where latency matters more than raw quality
_LATENCY_SENSITIVE = {"fast_chat", "creative", "coding"}
# Minimum samples before latency sort kicks in (avoids cold-start bias)
_LATENCY_SORT_MIN_N = 3


# =============================================================================
# Smart Router
# =============================================================================

class SmartRouter:
    def __init__(self):
        self.catalog         = _load_catalog()
        self._safe_params    = _build_safe_params(self.catalog)
        self.health          = HealthTracker(failure_threshold=3, cooldown_seconds=120)
        self.rate_limiter    = SlidingWindowRateLimiter(self.catalog)
        self.latency_tracker = LatencyTracker(window=20)
        self.ab_tests        = ABTestManager()
        # [Phase 28] Self-healing fallback-chain bias (set via bias_fallback_chain)
        self._bias_model:         str | None = None
        self._bias_until:         float      = 0.0
        self._bias_recover_check: set[str]   = set()
        # [Phase 29] Ollama reachability cache (avoid hot-path latency)
        self._ollama_health_cache:   bool | None = None
        self._ollama_health_checked: float       = 0.0
        self._inject_api_keys()
        self._seed_ab_experiments()
        active = settings.available_providers
        log.info(
            f"[router] SmartRouter v3 ready — "
            f"104 models / 14 providers — latency tracking + A/B tests active — "
            f"active: [cyan]{', '.join(active) if active else 'NONE — add keys to .env'}[/cyan]"
        )

    # -------------------------------------------------------------------------
    # Startup helpers
    # -------------------------------------------------------------------------

    def _seed_ab_experiments(self) -> None:
        """Pre-configured experiments that run from startup."""
        # fast_chat: Fireworks DeepSeek V3 vs Groq Llama — winner gets more traffic over time
        self.ab_tests.register(
            "fast_chat_ab",
            variants=[
                {"model": "fireworks_ai/accounts/fireworks/models/deepseek-v4-flash", "weight": 0.5},
                {"model": "groq/llama-3.3-70b-versatile",                       "weight": 0.35},
                {"model": "cerebras/llama-3.3-70b",                             "weight": 0.15},
            ],
            category="fast_chat",
            traffic_pct=1.0,
        )
        self.ab_tests.register(
            "coding_ab",
            variants=[
                {"model": "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",                         "weight": 0.6},
                {"model": "fireworks_ai/accounts/fireworks/models/deepseek-coder-v2-instruct",    "weight": 0.4},
            ],
            category="coding",
            traffic_pct=0.5,
        )

    # -------------------------------------------------------------------------
    # API key injection
    # -------------------------------------------------------------------------

    def _inject_api_keys(self) -> None:
        """Push all configured API keys into LiteLLM's os.environ."""
        key_map = {
            "GROQ_API_KEY":          settings.groq_api_key,
            "CEREBRAS_API_KEY":      settings.cerebras_api_key,
            "GEMINI_API_KEY":        settings.gemini_api_key,
            "GOOGLE_API_KEY":        settings.gemini_api_key,
            "OPENROUTER_API_KEY":    settings.openrouter_api_key,
            "TOGETHER_API_KEY":      settings.together_api_key,
            "FIREWORKS_AI_API_KEY":  settings.fireworks_ai_api_key,
            "DEEPINFRA_API_KEY":     settings.deepinfra_api_key,
            "MISTRAL_API_KEY":       settings.mistral_api_key,
            "COHERE_API_KEY":        settings.cohere_api_key,
            "PERPLEXITYAI_API_KEY":  settings.perplexityai_api_key,
            "DEEPSEEK_API_KEY":      settings.deepseek_api_key,
            "HUGGINGFACE_API_KEY":   settings.huggingface_api_key,
        }
        injected = []
        for env_var, value in key_map.items():
            if value:
                os.environ[env_var] = value
                injected.append(env_var.replace("_API_KEY", "").lower())
        if injected:
            log.info(f"[router] Injected keys for: {', '.join(injected)}")

    # -------------------------------------------------------------------------
    # Param filtering
    # -------------------------------------------------------------------------

    def _filter_params(self, model_id: str, kwargs: dict) -> dict:
        provider = model_id.split("/")[0]
        safe     = self._safe_params.get(provider, set())
        filtered = {k: v for k, v in kwargs.items() if k in safe}
        dropped  = set(kwargs) - set(filtered)
        if dropped:
            log.debug(f"[router] Dropped unsupported params for {provider}: {dropped}")
        return filtered

    def _get_chain(
        self,
        category:        str,
        preferred_model: str | None = None,
    ) -> list[str]:
        """
        Build an ordered fallback chain for *category*, applying in order:
          1. Availability filter  -- strip providers with no API key
          2. Latency sort         -- P50-sort latency-sensitive categories
          3. Self-healing bias    -- [Phase 28] favor a healthy model while
                                      circuit breakers are tripped elsewhere
          4. A/B override         -- inject A/B winner at front (when no preferred_model)
          5. Preferred model      -- force-front a specific model (intent hint)
        """
        cat = self.catalog["categories"].get(category)
        if not cat:
            log.warning(f"[router] Unknown category '{category}', using fast_chat")
            cat = self.catalog["categories"]["fast_chat"]

        chain     = [cat["primary"]] + cat.get("fallbacks", [])
        available = settings.available_providers
        filtered  = [m for m in chain if m.split("/")[0] in available]

        if not filtered:
            log.error(
                f"[router] No providers configured for category '{category}'! "
                f"Chain={chain} / Available={available}"
            )
            return chain

        # Step 1: Latency-aware reorder for speed-sensitive categories
        if category in _LATENCY_SENSITIVE:
            warmed     = [m for m in filtered if self.latency_tracker.stats(m)["n"] >= _LATENCY_SORT_MIN_N]
            not_warmed = [m for m in filtered if self.latency_tracker.stats(m)["n"] < _LATENCY_SORT_MIN_N]
            filtered   = self.latency_tracker.sort_by_latency(warmed) + not_warmed

        # Step 1.5 [Phase 28]: Self-healing bias -- temporarily favor a known-healthy
        # model within this chain while other models are circuit-tripped. Reorders
        # only within the already-validated/available `filtered` list, so provider
        # safe-param filtering (_filter_params) and availability are unaffected.
        self._check_bias_revert()
        if self._bias_model and self._bias_model in filtered and filtered[0] != self._bias_model:
            filtered = [self._bias_model] + [m for m in filtered if m != self._bias_model]

        # Step 2: A/B selection (only when no preferred_model is overriding)
        if not preferred_model:
            exp_id = self.ab_tests.experiment_for_category(category)
            if exp_id:
                variant = self.ab_tests.select(exp_id)
                if variant and variant in filtered:
                    filtered = [variant] + [m for m in filtered if m != variant]
                    log.debug(f"[router] A/B '{exp_id}' selected variant: {variant}")

        # Step 3: Preferred model goes unconditionally first
        if preferred_model:
            if preferred_model in filtered:
                filtered = [preferred_model] + [m for m in filtered if m != preferred_model]
            elif preferred_model.split("/")[0] in available:
                filtered = [preferred_model] + filtered
            log.debug(f"[router] Preferred model injected: {preferred_model}")

        return filtered

    # -------------------------------------------------------------------------
    # [Phase 28] Self-healing fallback-chain bias
    # -------------------------------------------------------------------------

    def bias_fallback_chain(self, prefer: str, ttl_seconds: int = 1800) -> dict:
        """
        Temporarily reorder fallback chains to favor `prefer` (a model
        believed healthy) while the circuit breaker has tripped on
        CIRCUIT_TRIP_REORDER_THRESHOLD+ models elsewhere.

        Auto-reverts via `_check_bias_revert()` (called on every `_get_chain`):
          - after `ttl_seconds` (default 30 min), OR
          - as soon as every model that was tripped at bias-time becomes
            available again (HealthTracker.is_available() == True), whichever
            comes first.

        Only reorders within each category's existing validated/available
        chain -- never injects a model that wouldn't otherwise be in the
        chain or that lacks a configured provider key.
        """
        self._bias_model         = prefer
        self._bias_until         = time.monotonic() + ttl_seconds
        self._bias_recover_check = set(self.health._tripped_at.keys())
        log.info(
            f"[router] Self-healing: biasing fallback chains toward "
            f"[bold]{prefer}[/bold] for up to {ttl_seconds}s "
            f"(or until {len(self._bias_recover_check)} tripped model(s) recover)"
        )
        return {
            "biased_model":          prefer,
            "ttl_seconds":           ttl_seconds,
            "watching_recovery_of":  list(self._bias_recover_check),
        }

    def _check_bias_revert(self) -> None:
        """Auto-revert an active self-healing bias once it expires or the
        models it was guarding against have recovered."""
        if not self._bias_model:
            return
        expired   = time.monotonic() >= self._bias_until
        recovered = bool(self._bias_recover_check) and all(
            self.health.is_available(m) for m in self._bias_recover_check
        )
        if expired or recovered:
            reason = "TTL expired" if expired else "tripped model(s) recovered"
            log.info(
                f"[router] Self-healing: reverting fallback-chain bias toward "
                f"{self._bias_model} ({reason})"
            )
            self._bias_model         = None
            self._bias_until         = 0.0
            self._bias_recover_check = set()

    def bias_status(self) -> dict:
        """Current self-healing bias state -- for diagnostics / HUD."""
        if not self._bias_model:
            return {"active": False}
        return {
            "active":               True,
            "biased_model":         self._bias_model,
            "remaining_s":          max(0, int(self._bias_until - time.monotonic())),
            "watching_recovery_of": list(self._bias_recover_check),
        }

    # -------------------------------------------------------------------------
    # [Phase 29] Ollama health check (cached -- never on the hot path)
    # -------------------------------------------------------------------------

    async def _check_ollama_health(self) -> bool:
        """GET {ollama_base_url}/api/tags with a short timeout (2s).

        Result is cached for `ollama_health_check_interval_sec` so a
        down/slow Ollama instance never adds latency to request handling --
        worst case we wait out one 2s timeout per interval, not per request.
        """
        now = time.monotonic()
        if (
            self._ollama_health_cache is not None
            and now - self._ollama_health_checked < settings.ollama_health_check_interval_sec
        ):
            return self._ollama_health_cache

        healthy = False
        try:
            client = httpx.AsyncClient(timeout=2.0)
            try:
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
                healthy = resp.status_code == 200
            finally:
                await client.aclose()
        except Exception as e:
            log.debug(f"[router] Ollama health check failed: {type(e).__name__}: {e}")
            healthy = False

        self._ollama_health_cache   = healthy
        self._ollama_health_checked = now
        return healthy

    async def _try_model(self, model_id: str, messages: list, stream: bool, kwargs: dict) -> Any:
        filtered = self._filter_params(model_id, kwargs)
        self.rate_limiter.record(model_id)
        return await litellm.acompletion(
            model=model_id,
            messages=messages,
            stream=stream,
            timeout=60,
            **filtered,
        )

    async def complete(
        self,
        messages:        list[dict],
        category:        str = "fast_chat",
        stream:          bool = False,
        preferred_model: str | None = None,
        **kwargs,
    ) -> tuple[Any, str]:
        chain      = self._get_chain(category, preferred_model=preferred_model)
        last_error = None

        ab_exp_id = (
            self.ab_tests.experiment_for_category(category)
            if not preferred_model else None
        )
        ab_variant_models = set(
            v["model"]
            for v in self.ab_tests._experiments.get(ab_exp_id or "", {}).get("variants", [])
        )

        for model_id in chain:
            if not self.health.is_available(model_id):
                log.debug(f"[router] Skip degraded: {model_id}")
                continue
            if self.rate_limiter.is_limited(model_id):
                log.debug(f"[router] Rate limited: {model_id}")
                continue

            try:
                log.info(f"[router] -> [cyan]{model_id}[/cyan] (category={category})")
                t0 = time.monotonic()
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(2),
                    wait=wait_exponential(multiplier=1, min=1, max=8),
                    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
                    reraise=True,
                ):
                    with attempt:
                        response = await self._try_model(model_id, messages, stream, kwargs)

                elapsed_ms = (time.monotonic() - t0) * 1000
                self.health.mark_success(model_id)
                self.latency_tracker.record(model_id, elapsed_ms)

                tokens = getattr(getattr(response, "usage", None), "total_tokens", 0) or 0
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, elapsed_ms, tokens, True)

                log.info(f"[router] OK {model_id} in [bold]{elapsed_ms:.0f}ms[/bold]")
                return response, model_id

            except litellm.RateLimitError as e:
                log.warning(f"[router] Rate limit on {model_id}: {e}")
                self.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                last_error = RateLimitError(model_id.split("/")[0], model_id, str(e))
            except litellm.AuthenticationError as e:
                log.error(f"[router] Auth error on {model_id} -- check .env key")
                self.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                last_error = ProviderError(model_id.split("/")[0], model_id, str(e), 401)
            except litellm.BadRequestError as e:
                log.error(f"[router] Bad request to {model_id}: {e}")
                self.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                last_error = ProviderError(model_id.split("/")[0], model_id, str(e), 400)
            except litellm.ServiceUnavailableError as e:
                log.warning(f"[router] {model_id} unavailable: {e}")
                self.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                last_error = ProviderError(model_id.split("/")[0], model_id, str(e), 503)
            except Exception as e:
                log.warning(f"[router] {model_id}: {type(e).__name__}: {e}")
                self.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                last_error = e

        # [Phase 29] Local Ollama mesh fallback -- only reached once every
        # cloud provider in `chain` has failed/is degraded/rate-limited.
        if settings.ollama_fallback_enabled and await self._check_ollama_health():
            primary = _OLLAMA_INTENT_MAP.get(category, _OLLAMA_DEFAULT_FALLBACK)
            candidates = [primary] + [m for m in _OLLAMA_FALLBACK_PRIORITY if m != primary]
            tried: set[str] = set()

            for fallback_model in candidates:
                if fallback_model in tried:
                    continue
                tried.add(fallback_model)
                if not self.health.is_available(fallback_model):
                    log.debug(f"[router] Skip degraded local fallback: {fallback_model}")
                    continue

                try:
                    log.warning(
                        f"[router] All cloud providers exhausted for category='{category}' "
                        f"-- falling back to local {fallback_model}"
                    )
                    t0 = time.monotonic()
                    response = await self._try_model(fallback_model, messages, stream, kwargs)
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    self.health.mark_success(fallback_model)
                    self.latency_tracker.record(fallback_model, elapsed_ms)
                    response = _apply_local_fallback_disclaimer(response, stream)
                    log.info(f"[router] OK (local fallback) {fallback_model} in {elapsed_ms:.0f}ms")
                    return response, fallback_model
                except Exception as e:
                    # Edge case: Ollama reachable but this model isn't pulled
                    # (or some other local error) -- mark degraded and try
                    # the next local candidate rather than failing outright.
                    log.warning(
                        f"[router] Local fallback {fallback_model} failed "
                        f"({type(e).__name__}: {e}) -- trying next local model"
                    )
                    self.health.mark_failure(fallback_model)
                    last_error = e

            log.error(
                f"[router] Ollama reachable but no usable local model for "
                f"category='{category}' (tried: {sorted(tried)}). Run "
                f"`python3 scripts/check_ollama.py` to see which models need `ollama pull`."
            )

        raise AllProvidersExhaustedError(
            f"All models exhausted for category='{category}'. Last: {last_error}"
        )

    def provider_status(self) -> dict:
        degraded  = self.health.status()
        available = settings.available_providers
        chains    = {}
        for cat, data in self.catalog["categories"].items():
            chain = [data["primary"]] + data.get("fallbacks", [])
            chains[cat] = [
                {
                    "model":               m,
                    "status":              degraded.get(m, "ok"),
                    "rpm_remaining":       self.rate_limiter.remaining(m),
                    "provider_configured": m.split("/")[0] in available,
                    "latency":             self.latency_tracker.stats(m),
                }
                for m in chain
            ]
        return {
            "configured_providers": available,
            "total_models":         104,
            "total_providers":      14,
            "chains":               chains,
            "ab_tests":             self.ab_tests.results(),
            "self_healing_bias":    self.bias_status(),
        }


# Singleton
_router: SmartRouter | None = None


def get_router() -> SmartRouter:
    global _router
    if _router is None:
        _router = SmartRouter()
    return _router
