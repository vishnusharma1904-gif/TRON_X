from __future__ import annotations

from src.intelligence.language_profile import build_language_profile
from src.intelligence.self_model import SelfModel


def test_language_profile_detects_telugu_script():
    profile = build_language_profile("నాకు calculus explain చేయి")
    assert profile["detected"] is True
    assert profile["dialect"] == "telugu_script"
    assert profile["preferred_tts_lang"] == "te"
    assert profile["reply_style"] == "telugu_unicode"


def test_language_profile_detects_tenglish():
    profile = build_language_profile("bro idi ela solve cheyyali for derivatives?")
    assert profile["detected"] is True
    assert profile["dialect"] in {"romanised", "tenglish"}
    assert profile["reply_style"] in {"romanised", "tenglish"}


def test_self_model_reflection_is_bounded():
    model = SelfModel()
    state = model.reflect(
        user_message="Need help with math and telugu rendering",
        intent="academic",
        emotion="confused",
        language_profile={"reply_style": "tenglish"},
    )
    assert state["mood"] == "confused"
    assert "simulated self-model" in state["reflection_summary"].lower()
    assert len(state["recent_priorities"]) <= 5
