"""
TRON-X Intent Classifier  (Phase 1 — Enhanced)
────────────────────────────────────────────────
Two-stage classification:
  1. Keyword heuristics  — instant, no API call, covers ~70% of cases
  2. LLM classification  — for ambiguous messages, uses fastest available model

Returns: (intent: str, confidence: float, method: str)

Telugu-language messages are handled gracefully — they fall through to
'chat' intent unless they match a specific domain keyword (coding, academic, etc.).
"""
from __future__ import annotations

import re
import time
from typing import Tuple

from src.core.config import get_settings
from src.core.logger import log
from src.intelligence.intent_cache import (
    MIN_CONFIDENCE_TO_STORE,
    SAFE_CACHEABLE_INTENTS,
    get_intent_cache,
)
from src.intelligence.prompts import INTENT_CLASSIFICATION_PROMPT

# ── Keyword heuristics ────────────────────────────────────────────────────────
# Each intent maps to a set of trigger patterns (lowercased).
# More specific patterns score higher. Order doesn't matter — all are checked.

_INTENT_PATTERNS: dict[str, list[str]] = {
    "coding": [
        "code", "function", "bug", "error", "debug", "implement", "refactor",
        "python", "javascript", "typescript", "java", "c++", "rust", "golang",
        "class", "method", "algorithm", "script", "program", "api", "json",
        "sql", "html", "css", "react", "fastapi", "django", "flask",
        "compile", "syntax", "import", "library", "framework", "git",
        "stack overflow", "null pointer", "segfault", "traceback", "exception",
        "dockerfile", "kubernetes", "docker", "bash script", "shell script",
        "unit test", "pytest", "jest", "type error", "attribute error",
    ],
    "academic": [
        "explain", "what is", "define", "concept", "theory", "exam",
        "study", "university", "semester", "assignment", "homework", "notes",
        "btech", "b.tech", "engineering", "physics", "chemistry", "biology",
        "circuit", "thermodynamics", "electromagnetics", "mechanics", "optics",
        "transistor", "semiconductor", "signal processing", "control systems",
        "exam pov", "exam point of view", "short answer", "long answer",
        "syllabus", "marks", "viva", "internals",
        "lecture notes", "reference book", "first principles",
    ],
    "medical": [
        "symptom", "diagnosis", "disease", "medicine", "drug", "treatment",
        "doctor", "patient", "health", "pain", "fever", "medication",
        "dose", "hospital", "clinical", "pathology", "physiology", "anatomy",
        "infection", "virus", "bacteria", "antibiotic", "surgery", "chronic",
        "acute", "differential diagnosis", "ddx", "side effect", "contraindication",
    ],
    "math": [
        "solve", "calculate", "equation", "integral", "derivative", "matrix",
        "probability", "statistics", "proof", "theorem", "formula", "calculus",
        "algebra", "trigonometry", "graph", "function plot", "eigenvalue",
        "fourier", "laplace", "differential equation", "limit", "series",
        "permutation", "combination", "bayes", "determinant", "vector",
    ],
    "reasoning": [
        "why", "how come", "analyze", "compare", "evaluate", "logical",
        "argument", "debate", "philosophy", "ethics", "decision", "trade-off",
        "pros and cons", "which is better", "should i", "what would happen",
        "think about", "make sense", "is it worth", "best approach",
    ],
    "vision": [
        "image", "photo", "picture", "screenshot", "diagram", "chart",
        "visual", "look at this", "describe this", "what do you see",
        "attached image", "this image", "in the image",
    ],
    "iot": [
        "turn on", "turn off", "switch on", "switch off", "light", "lights",
        "temperature", "thermostat", "hvac", "fan", "ac", "air conditioner",
        "lock", "unlock", "door", "sensor", "smart home", "home assistant",
        "device", "relay", "dimmer", "brightness of",
    ],
    "system": [
        "open app", "close app", "launch", "open file", "delete file",
        "rename", "move file", "copy file", "volume", "mute", "screenshot",
        "desktop", "task manager", "process", "install", "uninstall",
        "folder", "directory", "search file", "click on", "type in",
        "navigate to", "open browser", "control my", "take over",
    ],
    "cad": [
        "3d model", "design a", "cad model", "stl file", "step file",
        "mechanical part", "component", "assembly", "extrude", "revolve",
        "cadquery", "openscad", "sketch", "dimension", "tolerance",
        "3d print", "cnc",
    ],
    "research": [
        "research", "literature review", "comprehensive overview",
        "in depth", "deep dive", "survey", "state of the art",
        "compare and contrast", "detailed analysis", "write a report",
        "summarize the topic", "what are all the",
    ],
    "computer": [
        "click on", "click the", "open chrome", "open browser", "open firefox",
        "open google", "type in", "press enter", "scroll down", "scroll up",
        "take a screenshot", "take screenshot", "control my laptop", "control my computer",
        "control my screen", "drag and drop", "search google in browser",
        "open file", "launch app", "launch application", "move my mouse",
        "right click", "double click", "copy paste", "select all",
        "navigate to", "go to website", "open url", "open link",
        "switch window", "close window", "minimize window", "maximize window",
        "what's on my screen", "what is on my screen", "read my screen",
        "automate my", "do this on my computer", "perform on screen",
        "computer control", "desktop control", "keyboard shortcut",
        "ctrl c", "ctrl v", "alt tab", "win key", "windows key",
        "type for me", "click for me", "look at my screen",
    ],
    "creative": [
        "write a story", "write a poem", "creative writing", "brainstorm",
        "come up with", "think of ideas", "name ideas", "tagline",
        "pitch idea", "story idea", "script", "dialogue",
        "design concept", "creative brief",
    ],
}

# Telugu/Tenglish keywords that map to standard intents
_TELUGU_INTENT_OVERRIDES: dict[str, list[str]] = {
    "coding": [
        "code chesanu", "code cheyyi", "bug fix", "code pani",
        "program rayu", "error vasthundi",
    ],
    "academic": [
        "chaduvuko", "chadivo", "exam notes", "subject explain",
        "theory em", "concept em undi", "study material",
    ],
    "chat": [
        "em chestunnav", "ela unnav", "ki re bro", "enti jarigindi",
        "baaga unnava", "sup machcha",
    ],
}

# Minimum keyword matches to trigger an intent
_THRESHOLD = 1

# Intents that need CoT / are high-stakes → prefer LLM verification
_VERIFY_INTENTS = {"medical", "reasoning", "math"}


def _keyword_classify(message: str) -> tuple[str, float]:
    """
    Fast keyword-based classification.
    Returns (intent, confidence) where confidence is in [0, 1].
    """
    msg_lower = message.lower()
    scores: dict[str, int] = {}

    for intent, patterns in _INTENT_PATTERNS.items():
        count = sum(1 for p in patterns if p in msg_lower)
        if count >= _THRESHOLD:
            scores[intent] = count

    # Telugu override check
    for intent, patterns in _TELUGU_INTENT_OVERRIDES.items():
        count = sum(1 for p in patterns if p in msg_lower)
        if count >= _THRESHOLD:
            scores[intent] = scores.get(intent, 0) + count

    if not scores:
        return "chat", 0.5  # Default

    best_intent = max(scores, key=scores.__getitem__)
    best_score  = scores[best_intent]

    # Normalise confidence: 1 match → 0.65, 3+ matches → 0.90
    confidence = min(0.65 + (best_score - 1) * 0.10, 0.90)

    # If multiple intents scored, reduce confidence
    if len(scores) > 1:
        confidence = max(0.55, confidence - 0.10)

    return best_intent, confidence


async def _llm_classify(message: str, router) -> tuple[str, float]:
    """
    LLM-based classification using the fastest available model.
    """
    from src.intelligence.router import get_router
    r = router or get_router()

    prompt = INTENT_CLASSIFICATION_PROMPT.format(message=message[:500])
    messages = [{"role": "user", "content": prompt}]

    try:
        response, _ = await r.complete(
            messages=messages,
            category="fast_chat",
            temperature=0.0,
            max_tokens=10,
        )
        raw = response.choices[0].message.content.strip().lower()

        # Extract just the intent word
        valid_intents = set(_INTENT_PATTERNS.keys()) | {"chat"}
        for word in re.split(r"\W+", raw):
            if word in valid_intents:
                return word, 0.92

        log.debug(f"[intent] LLM returned unexpected intent: '{raw}' → defaulting to chat")
        return "chat", 0.5

    except Exception as e:
        log.warning(f"[intent] LLM classification failed: {e} → using keyword result")
        return "chat", 0.5


# ── Public classifier ─────────────────────────────────────────────────────────

class IntentClassifier:
    def __init__(self, router=None):
        self._router = router
        self._cache: dict[str, tuple[str, float]] = {}

    async def classify(
        self,
        message: str,
        force_llm: bool = False,
    ) -> tuple[str, float, str]:
        """
        Classify message intent.

        Returns:
            (intent, confidence, method)
            method is "keyword" | "llm" | "cache"
        """
        if not message or not message.strip():
            return "chat", 0.5, "default"

        t0 = time.monotonic()

        # Cache check (exact match only — good enough for repeated queries)
        cache_key = message.strip().lower()[:100]
        if cache_key in self._cache and not force_llm:
            intent, confidence = self._cache[cache_key]
            return intent, confidence, "cache"

        # Phase 22: semantic intent cache — embedding-similarity match against
        # prior high-confidence whitelisted classifications. A hit here skips
        # keyword heuristics AND any LLM call entirely (paraphrase routing).
        settings = get_settings()
        if settings.intent_cache_enabled and not force_llm:
            cached = await get_intent_cache().lookup(message)
            if cached is not None:
                self._cache[cache_key] = (cached.intent, cached.similarity)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                log.info(
                    f"[intent] '{message[:40]}…' → "
                    f"[bold cyan]{cached.intent}[/bold cyan] "
                    f"(conf={cached.similarity:.3f}, method=cache_semantic, {elapsed_ms}ms)"
                )
                return cached.intent, cached.similarity, "cache_semantic"

        # Stage 1: keyword heuristics
        kw_intent, kw_confidence = _keyword_classify(message)

        # Stage 2: LLM verification for ambiguous or high-stakes intents
        method = "keyword"
        intent, confidence = kw_intent, kw_confidence

        use_llm = (
            force_llm
            or kw_confidence < 0.70               # ambiguous
            or kw_intent in _VERIFY_INTENTS        # high-stakes → verify
        )

        if use_llm:
            llm_intent, llm_confidence = await _llm_classify(message, self._router)
            method = "llm"

            # Trust LLM if it's confident; blend if unsure
            if llm_confidence >= 0.85:
                intent, confidence = llm_intent, llm_confidence
            elif llm_intent == kw_intent:
                intent    = kw_intent
                confidence = min(0.95, (kw_confidence + llm_confidence) / 2 + 0.10)
            else:
                # Disagreement → trust LLM for high-stakes, keywords otherwise
                if kw_intent in _VERIFY_INTENTS or llm_intent in _VERIFY_INTENTS:
                    intent, confidence = llm_intent, llm_confidence
                else:
                    intent, confidence = kw_intent, kw_confidence

        # Phase 22: persist high-confidence whitelisted classifications so
        # future paraphrases hit the semantic cache above. For "iot", also
        # resolve+store the fast-path device action (if any) so nl_mapper
        # can dispatch it without an LLM call too (see intent_cache.py).
        if settings.intent_cache_enabled and confidence >= MIN_CONFIDENCE_TO_STORE \
                and intent in SAFE_CACHEABLE_INTENTS:
            resolved_action = None
            if intent == "iot":
                try:
                    from src.iot.nl_mapper import parse_command
                    resolved_action = parse_command(message)
                except Exception:
                    resolved_action = None
            await get_intent_cache().store(message, intent, resolved_action)

        self._cache[cache_key] = (intent, confidence)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            f"[intent] '{message[:40]}…' → "
            f"[bold cyan]{intent}[/bold cyan] "
            f"(conf={confidence:.2f}, method={method}, {elapsed_ms}ms)"
        )
        return intent, confidence, method
