"""
Phase 38 — Deliberative Reasoning Engine
Standalone test (conventions per TRONX_PHASE21-36_PROGRESS_HANDOFF.md §3).
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if "chromadb" not in sys.modules:
    sys.modules["chromadb"] = MagicMock()
    sys.modules["chromadb.config"] = MagicMock()

from src.intelligence import reasoning as rsn  # noqa: E402

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


def fake_orch_module(reply_fn):
    """Build a fake src.intelligence.orchestrator module whose chat() delegates
    to reply_fn(user_message, extra_system) -> str."""
    mod = MagicMock()
    orch = MagicMock()

    async def chat(**kw):
        return {"reply": reply_fn(kw.get("user_message", ""), kw.get("extra_system", ""))}

    orch.chat = AsyncMock(side_effect=chat)
    mod.get_orchestrator.return_value = orch
    return mod, orch


# =============================================================================
print("\n=== _strip_think / _normalise helpers ===")
# =============================================================================

check("strip think block",
      rsn._strip_think("<think>internal</think>  Paris") == "Paris")
check("strip dangling think",
      rsn._strip_think("<think>never closed... Paris") == "")
check("strip none", rsn._strip_think("Paris") == "Paris")
check("strip empty", rsn._strip_think("") == "")
check("normalise punctuation/case/space",
      rsn._normalise("  The Answer: 42! ") == rsn._normalise("the ANSWER 42"))


# =============================================================================
print("\n=== _parse_verdict robustness ===")
# =============================================================================

v = rsn._parse_verdict('{"verdict": "pass", "confidence": 0.9, "issue": ""}')
check("plain verdict parsed", v["verdict"] == "pass" and v["confidence"] == 0.9, v)

v = rsn._parse_verdict('```json\n{"verdict":"fail","confidence":0.2,"issue":"wrong"}\n```')
check("fenced verdict parsed", v["verdict"] == "fail" and v["issue"] == "wrong", v)

v = rsn._parse_verdict('Sure! Here: {"verdict": "pass", "confidence": 1.4,}')
check("prose + trailing comma + clamp",
      v["verdict"] == "pass" and v["confidence"] == 1.0, v)

v = rsn._parse_verdict("complete garbage")
check("garbage -> uncertain 0.5", v["verdict"] == "uncertain" and v["confidence"] == 0.5, v)

v = rsn._parse_verdict('{"verdict": "banana", "confidence": "x"}')
check("invalid fields coerced", v["verdict"] == "uncertain" and v["confidence"] == 0.5, v)


# =============================================================================
print("\n=== _vote majority logic ===")
# =============================================================================

ans, agree, votes = rsn.DeliberativeReasoner._vote(["Paris", "paris!", "London"])
check("majority wins", rsn._normalise(ans) == "paris", ans)
check("agreement 2/3", abs(agree - 2 / 3) < 1e-9, agree)
check("votes counted", sum(votes.values()) == 3, votes)

ans, agree, _ = rsn.DeliberativeReasoner._vote(["A"])
check("single candidate -> agreement 1.0", ans == "A" and agree == 1.0)


# =============================================================================
print("\n=== reason(): consensus path (no aggregation needed) ===")
# =============================================================================

async def _consensus_test():
    def reply(user_msg, system):
        if "verifier" in (system or "").lower() or '"verdict"' in (system or ""):
            return '{"verdict": "pass", "confidence": 0.9, "issue": ""}'
        if "reviewer" in (system or "").lower():
            return "<think>fine as is</think>Paris"
        return "<think>capital of France...</think>Paris"

    mod, orch = fake_orch_module(reply)
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=3).reason("Capital of France?")

    check("answer is Paris", out["answer"] == "Paris", out["answer"])
    check("3 samples used", out["samples"] == 3, out)
    check("full agreement", out["agreement"] == 1.0, out)
    check("no aggregate stage", "aggregate" not in out["trace"], out["trace"])
    check("reflect+verify in trace",
          "reflect" in out["trace"] and "verify" in out["trace"], out["trace"])
    check("confidence high (>0.7)", out["confidence"] > 0.7, out["confidence"])
    check("reflected False (answer unchanged)", out["reflected"] is False, out)

asyncio.run(_consensus_test())


# =============================================================================
print("\n=== reason(): split vote triggers aggregation ===")
# =============================================================================

async def _split_test():
    calls = {"n": 0}

    def reply(user_msg, system):
        sysl = (system or "").lower()
        if "aggregator" in sysl:
            return "AggregatedAnswer"
        if "reviewer" in sysl:
            return "AggregatedAnswer"
        if "verifier" in sysl:
            return '{"verdict": "pass", "confidence": 0.8, "issue": ""}'
        calls["n"] += 1
        return f"different-{calls['n']}"   # every sample disagrees

    mod, _ = fake_orch_module(reply)
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=3).reason("Hard question")

    check("aggregate stage ran", "aggregate" in out["trace"], out["trace"])
    check("aggregated answer used", out["answer"] == "AggregatedAnswer", out["answer"])
    check("agreement 1/3", abs(out["agreement"] - 1 / 3) < 1e-3, out)

asyncio.run(_split_test())


# =============================================================================
print("\n=== reason(): reflection can revise the answer ===")
# =============================================================================

async def _reflect_test():
    def reply(user_msg, system):
        sysl = (system or "").lower()
        if "reviewer" in sysl:
            return "<think>found an error</think>RevisedAnswer"
        if "verifier" in sysl:
            return '{"verdict": "pass", "confidence": 0.9, "issue": ""}'
        return "OriginalAnswer"

    mod, _ = fake_orch_module(reply)
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=2).reason("Q")

    check("reflected flag True", out["reflected"] is True, out)
    check("revised answer kept", out["answer"] == "RevisedAnswer", out["answer"])

asyncio.run(_reflect_test())


# =============================================================================
print("\n=== reason(): verifier 'fail' suppresses confidence ===")
# =============================================================================

async def _fail_verdict_test():
    def reply(user_msg, system):
        sysl = (system or "").lower()
        if "verifier" in sysl:
            return '{"verdict": "fail", "confidence": 0.9, "issue": "wrong"}'
        if "reviewer" in sysl:
            return "SameAnswer"
        return "SameAnswer"

    mod, _ = fake_orch_module(reply)
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=2).reason("Q")

    check("fail verdict recorded", out["verified"]["verdict"] == "fail", out["verified"])
    check("confidence suppressed (<=0.45)", out["confidence"] <= 0.45, out["confidence"])

asyncio.run(_fail_verdict_test())


# =============================================================================
print("\n=== reason(): total LLM failure degrades, never raises ===")
# =============================================================================

async def _llm_down_test():
    mod = MagicMock()
    orch = MagicMock()
    orch.chat = AsyncMock(side_effect=RuntimeError("llm down"))
    mod.get_orchestrator.return_value = orch

    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=3).reason("Q")

    check("no exception, fallback answer", isinstance(out["answer"], str) and out["answer"], out)
    check("zero confidence", out["confidence"] == 0.0, out)
    check("fallback stage in trace", "fallback_plain" in out["trace"], out["trace"])

asyncio.run(_llm_down_test())


# =============================================================================
print("\n=== reason(): samples=1, reflect/verify off (cheap mode) ===")
# =============================================================================

async def _cheap_test():
    n_calls = {"n": 0}

    def reply(user_msg, system):
        n_calls["n"] += 1
        return "OnlyAnswer"

    mod, _ = fake_orch_module(reply)
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.DeliberativeReasoner(samples=1, reflect=False, verify=False).reason("Q")

    check("exactly 1 LLM call", n_calls["n"] == 1, n_calls)
    check("answer returned", out["answer"] == "OnlyAnswer", out)
    check("confidence = agreement = 1.0", out["confidence"] == 1.0, out)
    check("verified is None", out["verified"] is None, out)

asyncio.run(_cheap_test())


# =============================================================================
print("\n=== deliberate() convenience wrapper ===")
# =============================================================================

async def _wrapper_test():
    mod, _ = fake_orch_module(lambda u, s: '{"verdict":"pass","confidence":0.9,"issue":""}'
                              if '"verdict"' in (s or "") else "W")
    with patch.dict(sys.modules, {"src.intelligence.orchestrator": mod}):
        out = await rsn.deliberate("Q", samples=2)
    check("wrapper returns dict with answer", out.get("answer") == "W", out)

asyncio.run(_wrapper_test())


# =============================================================================
print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")
sys.exit(1 if FAIL else 0)
