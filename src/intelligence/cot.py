"""
TRON-X Chain-of-Thought Handler
─────────────────────────────────
Injects CoT directives into system prompts for high-stakes intents.
Extracts and strips internal reasoning blocks from responses.
Applies structured output templates post-generation.
"""
from __future__ import annotations

import re

from src.core.logger import log

# ── Intents that get forced CoT ───────────────────────────────────────────────
COT_INTENTS = {"academic", "medical", "math", "reasoning", "cad", "coding"}

# ── CoT injection instructions ────────────────────────────────────────────────
_COT_BASE = """
## Reasoning Protocol
Before giving your final answer, reason through the problem step by step inside <think></think> tags.
Your thinking should be thorough and self-critical — check your work.
After the </think> tag, give only the clean, final answer. Do not repeat the thinking.
"""

_COT_VARIANTS: dict[str, str] = {
    "academic": _COT_BASE + """
In your thinking: identify the exact concept being tested, recall the relevant theorem/formula,
plan the derivation structure, then execute it. Check dimensional consistency for physics problems.
""",

    "medical": _COT_BASE + """
In your thinking: list the presenting features, generate a broad DDx, then narrow it systematically
using inclusion/exclusion criteria. Consider life-threatening diagnoses first.
Cross-check your management plan against standard clinical guidelines.
""",

    "math": _COT_BASE + """
In your thinking: identify the problem type, recall the relevant technique,
execute step by step, then verify your answer by substitution or estimation.
Check for edge cases and domain restrictions.
""",

    "reasoning": _COT_BASE + """
In your thinking: decompose the problem, identify hidden assumptions,
consider counterarguments, then synthesise a defensible conclusion.
""",

    "coding": _COT_BASE + """
In your thinking: understand the requirement, identify edge cases,
design the solution architecture, then implement. Review for bugs, security issues,
and performance before finalising.
""",

    "cad": _COT_BASE + """
In your thinking: determine the geometry needed, plan the parametric construction sequence,
identify any manufacturing constraints, then write the CadQuery code.
""",
}

# ── Think-block extraction ────────────────────────────────────────────────────
_THINK_RE = re.compile(
    r"<think>(.*?)</think>",
    re.DOTALL | re.IGNORECASE,
)


class CoTHandler:
    def inject(self, system_prompt: str, intent: str) -> str:
        """Append CoT instructions to the system prompt for qualifying intents."""
        if intent not in COT_INTENTS:
            return system_prompt

        cot_instructions = _COT_VARIANTS.get(intent, _COT_BASE)
        log.debug(f"[cot] Injecting CoT for intent='{intent}'")
        return system_prompt + "\n\n" + cot_instructions.strip()

    def extract_thinking(self, text: str) -> tuple[str, str | None]:
        """
        Split response into (visible_reply, hidden_thinking).

        The thinking block is stripped from the reply shown to the user
        but can be logged / stored for debugging.
        """
        match = _THINK_RE.search(text)
        if not match:
            return text.strip(), None

        thinking = match.group(1).strip()
        visible = _THINK_RE.sub("", text).strip()

        # Clean up leading/trailing whitespace left by removal
        visible = re.sub(r"\n{3,}", "\n\n", visible).strip()

        log.debug(f"[cot] Extracted thinking block ({len(thinking)} chars)")
        return visible, thinking

    def needs_cot(self, intent: str) -> bool:
        return intent in COT_INTENTS
