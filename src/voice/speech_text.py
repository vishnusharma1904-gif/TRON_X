"""
TRON-X Speech-Text Sanitizer  (Phase 38)
────────────────────────────────────────
Converts model output into clean speakable text. LaTeX and Unicode math
must never reach a synthesizer raw — "\\frac{a}{b}" spoken literally is
gibberish. Code blocks are for the screen, not the voice; emoji get
spelled out by TTS engines unless stripped.

Used by src/voice/tts.py for every synthesis call.
"""
from __future__ import annotations

import re

_PROSODY_MARKERS = re.compile(r"\[(pause|slow|fast|whisper|normal)\]", re.IGNORECASE)

_MATH_SPOKEN = {
    "≈": " approximately equal to ", "≠": " not equal to ",
    "≤": " less than or equal to ",  "≥": " greater than or equal to ",
    "±": " plus or minus ", "×": " times ", "÷": " divided by ",
    "√": " square root of ", "∞": " infinity ", "∑": " sum of ",
    "∫": " integral of ", "∂": " partial ", "Δ": " delta ", "δ": " delta ",
    "π": " pi ", "θ": " theta ", "λ": " lambda ", "μ": " mu ",
    "σ": " sigma ", "Ω": " omega ", "α": " alpha ", "β": " beta ",
    "γ": " gamma ", "→": " gives ", "⇒": " implies ", "∈": " in ",
    "²": " squared ", "³": " cubed ", "°": " degrees ", "·": " times ",
}

_LATEX_SPOKEN = [
    (re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}"), r"\1 over \2"),
    (re.compile(r"\\sqrt\s*\{([^{}]+)\}"),                r"square root of \1"),
    (re.compile(r"\\(left|right|,|;|!)"),                 " "),
    (re.compile(r"\\(times|cdot)\b"),                     " times "),
    (re.compile(r"\\(div)\b"),                            " divided by "),
    (re.compile(r"\\(pm)\b"),                             " plus or minus "),
    (re.compile(r"\\(approx)\b"),                         " approximately "),
    (re.compile(r"\\(neq|ne)\b"),                         " not equal to "),
    (re.compile(r"\\(leq|le)\b"),                         " less than or equal to "),
    (re.compile(r"\\(geq|ge)\b"),                         " greater than or equal to "),
    (re.compile(r"\\(infty)\b"),                          " infinity "),
    (re.compile(r"\\(pi|theta|lambda|mu|sigma|alpha|beta|gamma|delta|omega)\b"), r" \1 "),
    (re.compile(r"\\text\s*\{([^{}]*)\}"),                r"\1"),
    (re.compile(r"\\[a-zA-Z]+"),                          " "),   # leftover commands
    (re.compile(r"[{}_^]"),                               " "),
]

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")


def speakable_math(text: str) -> str:
    """Convert LaTeX/Unicode math into natural spoken words."""
    text = re.sub(r"\$\$(.+?)\$\$", r" \1 ", text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", r" \1 ", text, flags=re.DOTALL)
    text = re.sub(r"\$([^$\n]+)\$", r" \1 ", text)
    text = re.sub(r"\\\((.+?)\\\)", r" \1 ", text)
    for pat, repl in _LATEX_SPOKEN:
        text = pat.sub(repl, text)
    for sym, spoken in _MATH_SPOKEN.items():
        text = text.replace(sym, spoken)
    return text


def preprocess_for_speech(text: str) -> str:
    """Full markdown/math/emoji cleanup before synthesis."""
    text = re.sub(r"```.*?```", " — code shown on screen — ", text, flags=re.DOTALL)
    text = speakable_math(text)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", text)
    text = re.sub(r"#{1,6}\s", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = _PROSODY_MARKERS.sub("", text)
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
