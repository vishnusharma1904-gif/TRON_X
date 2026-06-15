from __future__ import annotations

from src.intelligence.telugu import TeluguState, detect_telugu


def build_language_profile(
    text: str,
    *,
    stt_language: str | None = None,
    telugu_state: TeluguState | None = None,
) -> dict:
    """Normalized language metadata shared across chat, search, and voice APIs."""
    state = telugu_state or detect_telugu(text)
    stt_lang = (stt_language or "").lower() or None
    detected = state.detected or stt_lang in {"te", "telugu"}
    dialect = state.dialect if state.detected else ("telugu_script" if stt_lang in {"te", "telugu"} else None)

    profile = {
        "detected": detected,
        "dialect": dialect,
        "confidence": state.confidence if state.detected else (0.75 if detected else 0.0),
        "stt_language": stt_lang,
        "stt_hint": "te" if detected else None,
        "preferred_tts_lang": (
            "te" if dialect == "telugu_script"
            # Tenglish / romanised / Hyderabadi → Indian-English neural voice:
            # pronounces code-mixed Latin-script Telugu naturally (Phase 38)
            else "en-IN" if dialect in {"romanised", "tenglish", "hyderabadi"}
            else "en"
        ),
        "reply_style": "english",
        "script_display": "unicode" if dialect == "telugu_script" else "latin",
    }

    if dialect == "telugu_script":
        profile["reply_style"] = "telugu_unicode"
    elif dialect in {"romanised", "tenglish", "hyderabadi"}:
        profile["reply_style"] = dialect

    return profile
