from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

_STATE_PATH = Path("memory/cache/voice_state.json")
_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


class VoiceStateStore:
    """Persistent voice-control state used by the HUD and voice APIs."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state = self._load()

    def _default_state(self) -> dict[str, Any]:
        return {
            "voice_output_enabled": settings.voice_output_default_enabled,
            "wake_word_enabled": settings.wake_word_enabled,
            "listening": False,
            "last_persona": "jarvis",
            "last_language_profile": {
                "detected": False,
                "dialect": None,
                "preferred_tts_lang": "en",
                "stt_hint": None,
                "reply_style": "english",
            },
            "updated_at": time.time(),
        }

    def _load(self) -> dict[str, Any]:
        if not _STATE_PATH.exists():
            return self._default_state()
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            merged = self._default_state()
            merged.update(data)
            return merged
        except Exception as exc:
            log.warning("[voice_state] Failed to load persisted state: %s", exc)
            return self._default_state()

    def _save(self) -> None:
        self._state["updated_at"] = time.time()
        _STATE_PATH.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def update(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(kwargs)
            self._save()
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def set_language_profile(self, profile: dict[str, Any], persona: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"last_language_profile": profile}
        if persona:
            payload["last_persona"] = persona
        return self.update(**payload)


_store: VoiceStateStore | None = None


def get_voice_state_store() -> VoiceStateStore:
    global _store
    if _store is None:
        _store = VoiceStateStore()
    return _store
