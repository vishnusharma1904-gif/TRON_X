"""
TRON-X Telugu Language Support  (Phase 1)
-------------------------------------------
Detects Telugu script, Romanised Telugu (transliterated), Tenglish
(Telugu-English code-mix), and Hyderabadi dialect (Telugu+Hindi/Urdu mix).

When Telugu is detected, both Jarvis and Friday inject Telugu-aware
response instructions and should reply in the same language style the
user is using (Telugu script / Romanised / Tenglish / Hyderabadi).

Supported dialects:
  telugu_script    — Unicode Telugu characters (0C00–0C7F)
  romanised        — Transliterated Telugu in Latin script
  tenglish         — Telugu+English code-switch
  hyderabadi       — Telugu+Hindi/Urdu mix (Hyderabadi dialect)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class TeluguState:
    detected:   bool  = False
    dialect:    str   = "none"         # telugu_script | romanised | tenglish | hyderabadi
    confidence: float = 0.0
    cues:       list[str] = None

    def __post_init__(self):
        if self.cues is None:
            self.cues = []

    def system_note(self) -> str:
        """Instruction block injected into system prompt when Telugu is detected."""
        if not self.detected:
            return ""
        dialect_guide = {
            "telugu_script": (
                "The user is writing in Telugu script. Respond in Telugu (use Telugu Unicode) "
                "mixed with English where technical terms require it. Be natural and conversational."
            ),
            "romanised": (
                "The user is writing in Romanised Telugu (transliterated). "
                "Respond in the same Romanised Telugu style — mix Telugu words written in "
                "English letters with English naturally. E.g. 'Idi fix chesanu bro, work avutundi.'"
            ),
            "tenglish": (
                "The user is writing in Tenglish (Telugu+English code-mix). "
                "Respond in Tenglish — weave Telugu words into English sentences naturally. "
                "E.g. 'Bro, idi baaga work avutundi — oka chinna issue matram unna, fix chesanu.'"
            ),
            "hyderabadi": (
                "The user is using Hyderabadi dialect (Telugu+Hindi/Urdu mix). "
                "Respond in the same Hyderabadi style — mix Telugu, Hindi/Urdu, and English freely. "
                "E.g. 'Bhai kya hua, idi fix ho gaya, tension mat le yaar.'"
            ),
        }
        guide = dialect_guide.get(self.dialect, "Respond naturally in the user's language style.")
        return (
            f"\n\n## Telugu Language Mode: {self.dialect} (confidence={self.confidence:.0%})\n"
            f"{guide}\n"
            "Use Telugu slang, expressions, and terms of address naturally:\n"
            "  'ra' / 'da' (masculine address), 'va' / 'le' (feminine), 'babu' (casual),\n"
            "  'machcha' (bro), 'anna' (elder brother), 'akka' (elder sister),\n"
            "  'entiki' (why), 'ela' (how), 'em' (what), 'cheyyi' (do it),\n"
            "  'baaga' (very/well), 'kadaa' (right?), 'aaaa' (oh I see),\n"
            "  'alaa' (like that), 'sarley' (okay/fine), 'chaduvuko' (study/learn),\n"
            "  'telisinda' (got it?), 'okate' (it's all the same), 'poyindi' (it's done/gone),\n"
            "  'ki re' (what's up / what is this), 'em chestunnav' (what are you doing).\n"
            "Mirror the user's energy — if they are casual, be casual; if formal Telugu, match it.\n"
            "NEVER translate everything to English if the user is clearly writing in Telugu/Tenglish."
        )


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Unicode Telugu block: U+0C00 to U+0C7F
_TELUGU_UNICODE_RE = re.compile(r"[ఀ-౿]")

# Common Romanised Telugu words (transliterated) — lowercase patterns
_ROMANISED_TELUGU_WORDS: list[str] = [
    # Pronouns / verbs / particles
    "nenu", "nenu", "meeru", "meerru", "meeru", "memu", "manam",
    "idi", "adi", "vadu", "vaadini", "aa vadu", "ayya",
    "cheyyi", "cheyya", "chesanu", "chestunna", "chestunnanu", "chesadu",
    "untundi", "undi", "leru", "ledu", "antu", "ante", "aina",
    "vastundi", "vastunna", "vastav", "vasthav",
    "poni", "pova", "potunna", "poyindi", "poindi",
    # Question words
    "em", "emi", "entiki", "enduku", "ela", "eppudu", "ekkada",
    "evadu", "evari", "evvaru", "evaraina",
    # Common expressions
    "baaga", "baagundi", "bavundi", "chala", "chaala",
    "machcha", "maccha", "anna", "akka", "nanna", "amma",
    "bujji", "babu", "mama", "maava",
    "sarley", "sarle", "sari", "okay ra", "okay da",
    "kadaa", "kadaa", "kaada", "kada",
    "aaaa", "ohhh", "aithe", "alaa", "ala antav",
    "telisinda", "telisi", "telusaa", "telusu",
    "okate", "okatey", "adey",
    "chaduvuko", "chadivo", "chaduvukunna",
    "ki re", "ki ra", "ki da", "entandi", "entaandi",
    "em chestunnav", "em chesav", "em aindi",
    "pani ledu", "pani chesav",
    "nuvvu", "mee", "nee", "meeru",
    "thervali", "thelusu", "thelusaa",
    "ayyo", "aiyyo", "arey", "are",
    "gaadu", "gadu", "vaadu", "laadu",
    "bro da", "bro ra", "bro enti", "boss ra",
    "yaar enti", "yaar em",
    "tight ga", "kick ga", "mass ga",
]

# Common Tenglish patterns — Telugu words dropped into English sentences
_TENGLISH_PATTERNS: list[str] = [
    r"\bbro\s+(ra|da|enti|em)\b",
    r"\b(sarle|sarley|sari)\b",
    r"\b(baaga|chaala|chala)\b",
    r"\b(machcha|maccha)\b",
    r"\b(anna|akka)\b.*\b(bro|dude|man)\b",
    r"\b(ayyo|aiyyo|arey)\b",
    r"\b(entiki|enduku|em)\b.*\?",
    r"\b(kadaa|kada|kaada)\b",
    r"\b(telisinda|telusaa)\b",
    r"\b(chaduvuko|chadivo)\b",
    r"\b(poyindi|poindi)\b",
    r"\b(chesanu|chestunna)\b",
    r"\b(tight\s*ga|kick\s*ga|mass\s*ga)\b",
]

# Hyderabadi dialect markers (Telugu + Hindi/Urdu)
_HYDERABADI_PATTERNS: list[str] = [
    r"\b(kya\s+re|kya\s+ra|kyaa)\b",
    r"\b(kya\s+hua|kya\s+hoga)\b",
    r"\byaar\b",
    r"\b(seedha|sidha)\b",
    r"\b(nakko|nai|nahi)\b.*\b(karo|karna)\b",
    r"\b(bol|bolo|bolre)\b",
    r"\bkya\s+(chahiye|chahte)\b",
    r"\b(mast|badhiya)\b",
    r"\b(lag\s*raha|lag\s*ra)\b",
    r"\b(bilkul|bilkull)\b",
    r"\bkoi\s+baat\s+nahi\b",
    r"\b(yaar|bhai)\b.*\b(ra|da)\b",
    r"\bhawa\s+aane\s+do\b",
]


class TeluguDetector:
    """Detects Telugu language variants in user messages."""

    _tenglish_re    = re.compile("|".join(_TENGLISH_PATTERNS),    re.I)
    _hyderabadi_re  = re.compile("|".join(_HYDERABADI_PATTERNS),  re.I)

    def detect(self, text: str) -> TeluguState:
        if not text or not text.strip():
            return TeluguState()

        text_lower = text.lower()
        cues: list[str] = []
        score = 0.0

        # ── 1. Telugu Unicode script (strongest signal) ────────────────────────
        telugu_chars = _TELUGU_UNICODE_RE.findall(text)
        if len(telugu_chars) >= 3:
            return TeluguState(
                detected=True,
                dialect="telugu_script",
                confidence=min(1.0, 0.5 + len(telugu_chars) * 0.05),
                cues=["unicode_telugu"],
            )

        # ── 2. Hyderabadi dialect check (before romanised — overlapping words) ─
        hyd_hits = self._hyderabadi_re.findall(text_lower)
        hyd_score = min(1.0, len(hyd_hits) * 0.35)

        # ── 3. Romanised Telugu word matching ─────────────────────────────────
        roman_hits = [w for w in _ROMANISED_TELUGU_WORDS if w in text_lower]
        roman_score = min(1.0, len(roman_hits) * 0.25)
        if roman_hits:
            cues.extend(roman_hits[:3])

        # ── 4. Tenglish pattern matching ───────────────────────────────────────
        tenglish_hits = self._tenglish_re.findall(text_lower)
        tenglish_score = min(1.0, len(tenglish_hits) * 0.30)
        if tenglish_hits:
            cues.append("tenglish_pattern")

        # ── 5. Determine dialect ───────────────────────────────────────────────
        scores = {
            "hyderabadi": hyd_score,
            "tenglish":   tenglish_score,
            "romanised":  roman_score,
        }
        best_dialect = max(scores, key=scores.__getitem__)
        best_score   = scores[best_dialect]

        if best_score < 0.25:
            return TeluguState()

        return TeluguState(
            detected=True,
            dialect=best_dialect,
            confidence=round(min(1.0, best_score), 2),
            cues=cues[:5],
        )


# ── Singleton ─────────────────────────────────────────────────────────────────

_tel_detector: TeluguDetector | None = None


def get_telugu_detector() -> TeluguDetector:
    global _tel_detector
    if _tel_detector is None:
        _tel_detector = TeluguDetector()
    return _tel_detector


def detect_telugu(text: str) -> TeluguState:
    """Convenience function — detect Telugu in a message."""
    return get_telugu_detector().detect(text)
