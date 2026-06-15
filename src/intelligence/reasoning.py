"""
TRON-X Deliberative Reasoning Engine  (Phase 38)
─────────────────────────────────────────────────
A reasoning layer that sits *above* the static CoT prompt-injection in
`cot.py`. Where `CoTHandler` only injects a "think step by step" directive,
the `DeliberativeReasoner` actively spends extra compute to raise answer
quality on hard questions, using three classic, well-understood techniques:

  1. Self-consistency  — sample N independent reasoning paths at a non-zero
     temperature, then pick the answer the paths agree on most (majority vote,
     with an LLM tie-break / aggregation pass when votes are split).
  2. Reflection        — take the chosen answer and run one self-critique pass
     ("find the strongest objection / error, then fix it"). Cheap, high-yield.
  3. Verification      — an independent check that the final answer actually
     satisfies the question's constraints, returning a confidence score.

Design constraints (so this stays testable + safe to ship):
  • Only `get_orchestrator().chat(...)` is used to reach an LLM — everything is
    mockable, exactly like `task_decomposer.py`.
  • The orchestrator import is lazy (inside functions) so importing this module
    never drags in litellm / chroma. Tests can patch `get_orchestrator`.
  • Every external call is wrapped; a failure degrades to a simpler mode rather
    than raising. The worst case is "one normal answer", never a crash.
  • `samples=1` collapses to a single reflect+verify pass (cheap default).

Public API:
    reasoner = DeliberativeReasoner(samples=3, reflect=True, verify=True)
    out = await reasoner.reason(question, persona="jarvis", session_id="...")
    # out = {answer, confidence, samples, agreement, votes, reflected, trace}
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any, Optional

from src.core.logger import log


# ── Prompts ────────────────────────────────────────────────────────────────────

_SAMPLE_SYSTEM = """\
You are a careful reasoner. Think through the problem step by step inside
<think></think> tags, checking your own work as you go. After </think>, output
ONLY the final answer on its own, with no preamble and no restatement of the
question. Be decisive: commit to a single best answer.
"""

_AGGREGATE_SYSTEM = """\
You are an answer aggregator. You are given a question and several independent
candidate answers produced by different reasoning attempts. Some may be wrong.
Weigh them, resolve contradictions, and produce the single best final answer.
Return ONLY the final answer — no commentary about the candidates.
"""

_REFLECT_SYSTEM = """\
You are a reviewer performing one critique-and-revise pass. You are given a
question and a proposed answer. Do the following silently inside
<think></think>: find the single strongest objection, error, missing case, or
unjustified leap in the proposed answer. If the answer is already correct and
complete, keep it. After </think>, output ONLY the final (possibly revised)
answer — no meta-commentary about what you changed.
"""

_VERIFY_SYSTEM = """\
You are a verifier. Given a question and a candidate answer, judge whether the
answer is correct and fully responsive to the question. Respond with ONLY a
JSON object of the exact shape:
{"verdict": "pass"|"fail"|"uncertain", "confidence": 0.0-1.0, "issue": "short reason or empty"}
No markdown, no other text.
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks and surrounding whitespace."""
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", text)
    # Drop any dangling, unclosed think opener.
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _normalise(text: str) -> str:
    """Aggressive normalisation used only for vote-bucketing (not display)."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s.\-]", "", t)        # drop punctuation noise
    return t.strip()


def _extract_json_object(text: str) -> Optional[str]:
    """First balanced {...} substring, or None."""
    start = (text or "").find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_verdict(reply: str) -> dict:
    """Parse the verifier's JSON, degrading to 'uncertain' on any problem."""
    import json
    raw = _extract_json_object(reply or "")
    if raw:
        for text in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
            try:
                data = json.loads(text)
            except Exception:
                continue
            if isinstance(data, dict) and "verdict" in data:
                verdict = str(data.get("verdict", "uncertain")).lower()
                if verdict not in ("pass", "fail", "uncertain"):
                    verdict = "uncertain"
                try:
                    conf = float(data.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                conf = max(0.0, min(1.0, conf))
                return {"verdict": verdict, "confidence": conf,
                        "issue": str(data.get("issue", ""))[:300]}
    return {"verdict": "uncertain", "confidence": 0.5, "issue": ""}


# ── Engine ─────────────────────────────────────────────────────────────────────

class DeliberativeReasoner:
    """
    Multi-sample, self-checking reasoning over the existing orchestrator.

    Parameters
    ----------
    samples : int
        Number of independent reasoning paths (>=1). 1 = single path.
    reflect : bool
        Run a critique-and-revise pass on the chosen answer.
    verify : bool
        Run an independent verification pass producing a confidence score.
    temperature : float
        Sampling temperature for the reasoning paths (diversity).
    max_tokens : int
        Token budget per LLM call.
    """

    def __init__(
        self,
        samples: int = 3,
        reflect: bool = True,
        verify: bool = True,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        intent: str = "reasoning",
    ):
        self.samples = max(1, int(samples))
        self.reflect = bool(reflect)
        self.verify = bool(verify)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.intent = intent

    # -- low-level LLM call -----------------------------------------------------

    async def _ask(
        self,
        user_message: str,
        system: str,
        session_id: str,
        persona: str,
        intent: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """One orchestrator round-trip. Returns the raw reply ('' on failure)."""
        from src.intelligence.orchestrator import get_orchestrator
        try:
            orch = get_orchestrator()
            res = await orch.chat(
                user_message=user_message,
                session_id=session_id,
                intent=intent or self.intent,
                persona=persona,
                max_tokens=self.max_tokens,
                temperature=self.temperature if temperature is None else temperature,
                extra_system=system,
            )
            return res.get("reply", "") or ""
        except Exception as e:
            log.warning(f"[reasoning] LLM call failed: {e}")
            return ""

    # -- stages -----------------------------------------------------------------

    async def _sample_paths(self, question: str, persona: str, session_id: str) -> list[str]:
        """Generate N independent answers concurrently. Empty results dropped."""
        async def one(i: int) -> str:
            reply = await self._ask(
                question, _SAMPLE_SYSTEM, f"{session_id}#s{i}", persona,
            )
            return _strip_think(reply)

        replies = await asyncio.gather(*[one(i) for i in range(self.samples)])
        return [r for r in replies if r]

    async def _aggregate(self, question: str, candidates: list[str],
                         persona: str, session_id: str) -> str:
        """LLM tie-break when the vote is split. Falls back to first candidate."""
        listing = "\n".join(f"Candidate {i + 1}: {c}" for i, c in enumerate(candidates))
        reply = await self._ask(
            f"Question:\n{question}\n\nCandidate answers:\n{listing}",
            _AGGREGATE_SYSTEM, f"{session_id}#agg", persona, temperature=0.2,
        )
        out = _strip_think(reply)
        return out or candidates[0]

    async def _reflect_once(self, question: str, answer: str,
                            persona: str, session_id: str) -> str:
        """Critique-and-revise. Returns possibly-improved answer."""
        reply = await self._ask(
            f"Question:\n{question}\n\nProposed answer:\n{answer}",
            _REFLECT_SYSTEM, f"{session_id}#reflect", persona, temperature=0.3,
        )
        out = _strip_think(reply)
        return out or answer

    async def _verify_once(self, question: str, answer: str,
                           persona: str, session_id: str) -> dict:
        """Independent verification → {verdict, confidence, issue}."""
        reply = await self._ask(
            f"Question:\n{question}\n\nCandidate answer:\n{answer}",
            _VERIFY_SYSTEM, f"{session_id}#verify", persona, temperature=0.0,
        )
        return _parse_verdict(reply)

    # -- vote -------------------------------------------------------------------

    @staticmethod
    def _vote(candidates: list[str]) -> tuple[str, float, dict]:
        """
        Majority vote over normalised candidates. Returns
        (winning_original_text, agreement_ratio, vote_counts_by_normalised).
        Ties broken by first-seen order (stable).
        """
        buckets: dict[str, str] = {}      # normalised -> first original
        counts: Counter = Counter()
        for c in candidates:
            key = _normalise(c)
            counts[key] += 1
            buckets.setdefault(key, c)
        # most_common is stable for equal counts in CPython 3.7+
        top_key, top_n = counts.most_common(1)[0]
        agreement = top_n / max(1, len(candidates))
        return buckets[top_key], agreement, dict(counts)

    # -- public -----------------------------------------------------------------

    async def reason(
        self,
        question: str,
        persona: str = "jarvis",
        session_id: str = "__reasoner__",
        intent: Optional[str] = None,
    ) -> dict:
        """
        Run the full deliberative pipeline.

        Returns a dict:
          answer      : final answer string
          confidence  : 0..1 (from verification, or agreement if verify off)
          samples     : number of non-empty reasoning paths used
          agreement   : majority-vote agreement ratio (0..1)
          votes       : {normalised_answer: count}
          reflected   : whether the reflection pass changed the answer
          verified    : verifier output dict (or None)
          trace       : list of stage names executed (for debugging/HUD)
        """
        trace: list[str] = []

        # 1. Sample independent reasoning paths.
        trace.append("sample")
        candidates = await self._sample_paths(question, persona, session_id)
        if not candidates:
            # Total failure of every path — single best-effort plain attempt.
            trace.append("fallback_plain")
            plain = _strip_think(await self._ask(
                question, _SAMPLE_SYSTEM, session_id, persona, intent=intent, temperature=0.2))
            return {
                "answer": plain or "I was unable to produce an answer.",
                "confidence": 0.0 if not plain else 0.3,
                "samples": 0, "agreement": 0.0, "votes": {},
                "reflected": False, "verified": None, "trace": trace,
            }

        # 2. Vote.
        trace.append("vote")
        answer, agreement, votes = self._vote(candidates)

        # 3. If paths disagree (and we actually had several), aggregate.
        if len(candidates) > 1 and agreement < 0.5:
            trace.append("aggregate")
            answer = await self._aggregate(question, candidates, persona, session_id)

        # 4. Reflection pass.
        reflected = False
        if self.reflect:
            trace.append("reflect")
            revised = await self._reflect_once(question, answer, persona, session_id)
            reflected = _normalise(revised) != _normalise(answer)
            answer = revised

        # 5. Verification pass → confidence.
        verified = None
        confidence = agreement
        if self.verify:
            trace.append("verify")
            verified = await self._verify_once(question, answer, persona, session_id)
            # Blend: verifier confidence is primary; sample agreement nudges it.
            base = verified["confidence"]
            if verified["verdict"] == "pass":
                confidence = min(1.0, 0.5 * base + 0.5 * (0.5 + 0.5 * agreement))
            elif verified["verdict"] == "fail":
                confidence = min(confidence, base * 0.5)
            else:
                confidence = 0.5 * base + 0.5 * agreement

        return {
            "answer": answer,
            "confidence": round(float(confidence), 3),
            "samples": len(candidates),
            "agreement": round(float(agreement), 3),
            "votes": votes,
            "reflected": reflected,
            "verified": verified,
            "trace": trace,
        }


# ── Module-level convenience ────────────────────────────────────────────────────

async def deliberate(
    question: str,
    samples: int = 3,
    reflect: bool = True,
    verify: bool = True,
    persona: str = "jarvis",
    session_id: str = "__reasoner__",
    intent: str = "reasoning",
) -> dict:
    """One-shot helper mirroring DeliberativeReasoner(...).reason(...)."""
    return await DeliberativeReasoner(
        samples=samples, reflect=reflect, verify=verify, intent=intent
    ).reason(question, persona=persona, session_id=session_id, intent=intent)
