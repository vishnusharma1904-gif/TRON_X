"""
Phase 38 — Jarvis/Friday-level capabilities test suite.

Covers: math-to-speech sanitizer (LaTeX/Unicode never spoken raw),
Telugu/Tenglish TTS language routing, self-model affect engine
(empathic coupling, decay, persistence contract, system_note honesty),
and the /api/self endpoints.

No network, no LLM, no audio synthesis.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════════════
# Math-to-speech sanitizer
# ═══════════════════════════════════════════════════════════════════════════

class TestSpeakableMath:
    def _pre(self, text):
        from src.voice.speech_text import preprocess_for_speech
        return preprocess_for_speech(text)

    def test_frac_spoken_as_over(self):
        out = self._pre(r"The answer is $\frac{a}{b}$ exactly.")
        assert "frac" not in out and "{" not in out
        assert "a over b" in out

    def test_sqrt_spoken(self):
        out = self._pre(r"so $\sqrt{16} = 4$")
        assert "square root of 16" in out and "\\" not in out

    def test_unicode_symbols_spoken(self):
        out = self._pre("x² ≈ 9.8, θ = π/2")
        assert "squared" in out and "approximately equal to" in out
        assert "theta" in out and "pi" in out
        assert "²" not in out and "≈" not in out

    def test_display_math_unwrapped(self):
        out = self._pre("$$E = mc^2$$")
        assert "$$" not in out and "E = mc 2" in out.replace("  ", " ")

    def test_leftover_latex_commands_dropped(self):
        out = self._pre(r"$\int_0^1 \mathrm{d}x$")
        assert "\\" not in out and "{" not in out

    def test_code_blocks_not_spoken(self):
        out = self._pre("Here:\n```python\nprint('hi')\n```\nDone.")
        assert "print" not in out
        assert "code shown on screen" in out

    def test_emoji_stripped(self):
        out = self._pre("Great job! 🎉🚀 All done ✅")
        assert "🎉" not in out and "🚀" not in out and "✅" not in out
        assert "Great job!" in out

    def test_plain_text_untouched(self):
        out = self._pre("Hello sir, the weather is 31 degrees today.")
        assert out == "Hello sir, the weather is 31 degrees today."


# ═══════════════════════════════════════════════════════════════════════════
# Telugu / Tenglish TTS routing
# ═══════════════════════════════════════════════════════════════════════════

class TestLanguageRouting:
    def test_telugu_script_routes_te(self):
        from src.intelligence.language_profile import build_language_profile
        p = build_language_profile("నమస్కారం, ఎలా ఉన్నారు?")
        assert p["detected"] is True
        assert p["preferred_tts_lang"] == "te"
        assert p["reply_style"] == "telugu_unicode"

    def test_tenglish_routes_indian_english(self):
        from src.intelligence.language_profile import build_language_profile
        from src.intelligence.telugu import TeluguState
        state = TeluguState(detected=True, dialect="tenglish", confidence=0.9)
        p = build_language_profile("em chestunnav bro", telugu_state=state)
        assert p["preferred_tts_lang"] == "en-IN"
        assert p["reply_style"] == "tenglish"

    def test_romanised_and_hyderabadi_route_indian_english(self):
        from src.intelligence.language_profile import build_language_profile
        from src.intelligence.telugu import TeluguState
        for dialect in ("romanised", "hyderabadi"):
            state = TeluguState(detected=True, dialect=dialect, confidence=0.8)
            p = build_language_profile("x", telugu_state=state)
            assert p["preferred_tts_lang"] == "en-IN", dialect

    def test_plain_english_unchanged(self):
        from src.intelligence.language_profile import build_language_profile
        p = build_language_profile("What's the weather like today?")
        assert p["preferred_tts_lang"] == "en"
        assert p["reply_style"] == "english"


# ═══════════════════════════════════════════════════════════════════════════
# Self-model affect engine
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def fresh_model(tmp_path, monkeypatch):
    """Isolated SelfModel writing to a temp state file."""
    import src.intelligence.self_model as sm
    monkeypatch.setattr(sm, "_SELF_MODEL_PATH", tmp_path / "self.json")
    sm._instance = None
    return sm.get_self_model()


def _reflect(m, emotion="neutral", intent="chat", msg="hello"):
    return m.reflect(user_message=msg, intent=intent, emotion=emotion,
                     language_profile={"reply_style": "english"})


class TestSelfModel:
    def test_v1_contract_preserved(self, fresh_model):
        state = _reflect(fresh_model)
        for key in ("mood", "recent_priorities", "reflection_summary",
                    "valence", "arousal"):
            assert key in state
        assert "not consciousness" in state["reflection_summary"]

    def test_positive_emotion_lifts_valence(self, fresh_model):
        v0 = fresh_model.valence
        _reflect(fresh_model, emotion="joy")
        assert fresh_model.valence > v0

    def test_negative_emotion_lowers_valence_raises_arousal(self, fresh_model):
        v0, a0 = fresh_model.valence, fresh_model.arousal
        _reflect(fresh_model, emotion="frustration")
        assert fresh_model.valence < v0 and fresh_model.arousal > a0

    def test_mood_decays_toward_baseline(self, fresh_model):
        import src.intelligence.self_model as sm
        _reflect(fresh_model, emotion="joy")
        high = fresh_model.valence
        # simulate two half-lives passing
        fresh_model._last_affect_update -= 2 * sm._MOOD_HALF_LIFE_S
        fresh_model._decay_affect()
        assert abs(fresh_model.valence - sm._BASELINE_VALENCE) < \
               abs(high - sm._BASELINE_VALENCE)

    def test_valence_clamped(self, fresh_model):
        for _ in range(50):
            _reflect(fresh_model, emotion="joy", msg="amazing!")
        assert fresh_model.valence <= 1.0

    def test_priorities_capped_at_five(self, fresh_model):
        for i in range(8):
            _reflect(fresh_model, intent=f"i{i}", msg=f"task {i}")
        assert len(fresh_model.get()["recent_priorities"]) == 5

    def test_lifetime_interactions_accumulate(self, fresh_model):
        for _ in range(3):
            _reflect(fresh_model)
        assert fresh_model.get()["lifetime_interactions"] == 3

    def test_system_note_is_honest_and_compact(self, fresh_model):
        _reflect(fresh_model, emotion="joy")
        note = fresh_model.system_note()
        assert "not sentience" in note
        assert "Mood:" in note and "Uptime" in note
        assert len(note) < 900   # must stay cheap in the prompt

    def test_get_includes_capabilities(self, fresh_model):
        caps = fresh_model.get()["capabilities"]
        assert "voice in English, Telugu and Tenglish" in caps["skills"]

    @pytest.mark.asyncio
    async def test_deep_reflect_persists_and_publishes(self, fresh_model):
        fake_orch = AsyncMock()
        fake_orch.chat = AsyncMock(return_value={"reply": "I had a calm day."})
        fake_chroma = AsyncMock()
        orch_mod = types.ModuleType("fake_o")
        orch_mod.get_orchestrator = lambda: fake_orch
        chroma_mod = types.ModuleType("fake_c")
        chroma_mod.get_chroma = lambda: fake_chroma
        with patch.dict(sys.modules, {
            "src.intelligence.orchestrator": orch_mod,
            "src.memory.chroma_db": chroma_mod,
        }):
            out = await fresh_model.deep_reflect()
        assert out["text"] == "I had a calm day."
        fake_chroma.remember_fact.assert_awaited_once()
        # journaled state survives in get()
        assert fresh_model.get()["last_deep_reflection"]["text"] == \
            "I had a calm day."


# ═══════════════════════════════════════════════════════════════════════════
# /api/self endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestSelfAPI:
    @pytest.fixture()
    def client(self, fresh_model):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.self import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_state_endpoint(self, client):
        r = client.get("/api/self/state")
        assert r.status_code == 200
        body = r.json()
        assert "mood" in body and "valence" in body and "capabilities" in body

    def test_reflect_endpoint(self, client, fresh_model):
        fake_orch = AsyncMock()
        fake_orch.chat = AsyncMock(return_value={"reply": "Reflected."})
        orch_mod = types.ModuleType("fake_o")
        orch_mod.get_orchestrator = lambda: fake_orch
        chroma_mod = types.ModuleType("fake_c")
        chroma_mod.get_chroma = lambda: AsyncMock()
        with patch.dict(sys.modules, {
            "src.intelligence.orchestrator": orch_mod,
            "src.memory.chroma_db": chroma_mod,
        }):
            r = client.post("/api/self/reflect")
        assert r.status_code == 200
        assert r.json()["text"] == "Reflected."
