"""
TRON-X Persona Engine  (Phase 1 — Enhanced)
──────────────────────────────────────────────
Manages Jarvis (alpha male best-friend tech genius) and Friday
(action queen, loving, caring, high-energy) personalities.

Builds dynamic system prompts by combining:
  1. Persona base prompt (from config/personas.json)
  2. Intent-specific extension (from prompts.py)
  3. Emotion-aware instruction (from emotion.py EmotionState)
  4. Telugu language instruction (from telugu.py TeluguState)
  5. Anti-filler instruction
  6. Optional RAG context injection

Response post-processing:
  - Strip <think> / CoT blocks
  - Strip banned filler openers
  - Enforce persona-specific style
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.core.logger import log
from src.intelligence.prompts import (
    ANTI_FILLER,
    EMOTION_PROMPTS,
    INTENT_EXTENSIONS,
    RAG_CONTEXT_TEMPLATE,
)

# ── Load persona configs ───────────────────────────────────────────────────────
_PERSONAS_FILE = Path("config/personas.json")


def _load_personas() -> dict:
    return json.loads(_PERSONAS_FILE.read_text(encoding="utf-8"))


# ── Filler / think-block patterns ─────────────────────────────────────────────

_FILLER_PATTERNS = re.compile(
    r"^(Certainly!?|Of course!?|Sure!?|Absolutely!?|Great question!?|"
    r"Happy to help!?|I'?d be happy to|I'?m glad you asked|"
    r"That'?s a great (question|point)|No problem!?|"
    r"I understand|I see\.|Interesting question!?|"
    r"As an AI[,\s]|As a language model[,\s]|"
    r"I understand your concern|Of course I can help)\s*[,.]?\s*",
    re.IGNORECASE,
)

_THINK_BLOCK_PATTERN = re.compile(
    r"<think>.*?</think>",
    re.DOTALL | re.IGNORECASE,
)

# Strip multiple trailing/leading newlines
_EXCESS_NEWLINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# PersonaEngine
# ---------------------------------------------------------------------------

class PersonaEngine:
    def __init__(self):
        self._personas = _load_personas()
        self._active: str = "jarvis"
        log.info(f"[persona] Loaded: {list(self._personas.keys())}")

    def set_persona(self, persona: str) -> None:
        if persona not in self._personas:
            log.warning(f"[persona] Unknown persona '{persona}', keeping '{self._active}'")
            return
        self._active = persona
        log.info(f"[persona] Switched to: {persona.upper()}")

    def get_persona_name(self, persona: str | None = None) -> str:
        p = persona or self._active
        return self._personas.get(p, {}).get("name", "TRON-X")

    def get_voice_id(self, persona: str | None = None) -> str:
        p = persona or self._active
        return self._personas.get(p, {}).get("voice_id", "en-GB-RyanNeural")

    def get_persona_style_note(self, persona: str | None = None) -> str:
        """Return a short style hint string for injection into sub-agent prompts."""
        p = (persona or self._active).lower()
        if p == "friday":
            return (
                "You speak with high energy, precision, and action-first attitude. "
                "Be direct, decisive, and get things done without hesitation."
            )
        return (
            "You speak with sharp intelligence, technical authority, and calm confidence. "
            "Be precise, efficient, and a few words ahead of what the user expects."
        )

    def build_system_prompt(
        self,
        intent:             str = "chat",
        persona:            str | None = None,
        rag_context:        str | None = None,
        extra_instructions: str | None = None,
        emotion_state=None,   # EmotionState | None
        telugu_state=None,    # TeluguState  | None
    ) -> str:
        """
        Assemble the full system prompt for a given intent, persona,
        emotion state, and language state.

        Structure:
            [Persona base prompt]
            [Intent-specific extension]
            [Emotion-aware instruction  — if emotion detected]
            [Telugu language instruction — if Telugu detected]
            [Anti-filler rules]
            [RAG context if provided]
            [Extra instructions if provided]
        """
        p = persona or self._active
        persona_cfg = self._personas.get(p, self._personas.get("jarvis"))

        parts: list[str] = [persona_cfg["base_prompt"].strip()]

        # Intent extension
        if intent in INTENT_EXTENSIONS:
            parts.append(INTENT_EXTENSIONS[intent].strip())

        # Emotion-aware instruction
        if emotion_state is not None and not emotion_state.is_neutral:
            emotion_name = emotion_state.primary.value
            if emotion_name in EMOTION_PROMPTS:
                parts.append(EMOTION_PROMPTS[emotion_name].strip())
            # Also append the raw hint line for context
            hint = emotion_state.persona_hint()
            if hint:
                parts.append(hint)

        # Telugu language instruction
        if telugu_state is not None and telugu_state.detected:
            note = telugu_state.system_note()
            if note:
                parts.append(note.strip())

        # Anti-filler
        parts.append(ANTI_FILLER.strip())

        # RAG context
        if rag_context and rag_context.strip():
            parts.append(RAG_CONTEXT_TEMPLATE.format(context=rag_context).strip())

        # Extra instructions (e.g. from scheduled tasks, tool results)
        if extra_instructions:
            parts.append(extra_instructions.strip())

        return "\n\n".join(parts)

    # ── Response post-processing ───────────────────────────────────────────────

    def sanitize_response(self, text: str, persona: str | None = None) -> str:
        """
        Post-process LLM output:
          1. Strip <think> blocks (DeepSeek R1 / CoT traces)
          2. Strip filler openers
          3. Clean up excess whitespace
        """
        if not text:
            return text

        # Strip thinking blocks
        text = _THINK_BLOCK_PATTERN.sub("", text).strip()

        # Strip filler openers (up to 3 iterations — some models stack them)
        for _ in range(3):
            stripped = _FILLER_PATTERNS.sub("", text, count=1).strip()
            if stripped == text:
                break
            text = stripped

        # Collapse triple+ newlines
        text = _EXCESS_NEWLINES.sub("\n\n", text)

        return text.strip()

    def format_for_exam(self, text: str) -> str:
        """Ensure the response follows exam POV structure if not already formatted."""
        if "##" in text or "**Definition**" in text:
            return text
        return (
            "**Answer:**\n\n" + text +
            "\n\n---\n*Formatted for exam revision.*"
        )
