"""
TRON-X Wake Word Detection
───────────────────────────
Uses openWakeWord for always-on detection.
Listens on a background thread — zero blocking.

Wake phrases:
  JARVIS mode: "hey jarvis", "jarvis"
  FRIDAY mode: "hey friday", "friday"

When triggered → fires a callback → orchestrator handles the request.

Install: pip install openwakeword
Models auto-downloaded on first run (~5MB each).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from src.core.logger import log

# Wake word → persona mapping
WAKE_WORDS = {
    "hey_jarvis": "jarvis",
    "jarvis":     "jarvis",
    "hey_friday": "friday",
    "friday":     "friday",
}

# Confidence threshold (0–1)
DETECTION_THRESHOLD = 0.5
# Cooldown between detections (seconds) — prevents double-triggers
COOLDOWN_SECONDS = 2.0


class WakeWordDetector:
    def __init__(self):
        self._model = None
        self._ready = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_trigger = 0.0
        self._callback: Optional[Callable] = None
        self._init()

    def _init(self) -> None:
        try:
            import openwakeword
            from openwakeword.model import Model
            # Load models for all wake phrases
            self._model = Model(
                wakeword_models=list(WAKE_WORDS.keys()),
                inference_framework="onnx",
            )
            self._ready = True
            log.info(f"[wake_word] openWakeWord ready ✓  phrases: {list(WAKE_WORDS.keys())}")
        except ImportError:
            log.warning(
                "[wake_word] openWakeWord not installed — wake word detection disabled. "
                "Install: pip install openwakeword"
            )
        except Exception as e:
            log.warning(f"[wake_word] Init failed: {e} — wake word detection disabled")

    def set_callback(self, callback: Callable[[str, float], None]) -> None:
        """
        Set function to call when wake word detected.
        Signature: callback(persona: str, confidence: float)
        """
        self._callback = callback

    def start(self) -> bool:
        """Start background listening thread. Returns True if started."""
        if not self._ready:
            log.warning("[wake_word] Cannot start — model not loaded")
            return False
        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        log.info("[wake_word] Listening for wake words...")
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        log.info("[wake_word] Stopped")

    def _listen_loop(self) -> None:
        try:
            import sounddevice as sd
            import numpy as np

            SAMPLE_RATE = 16000
            CHUNK_SIZE  = 1280  # 80ms chunks

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=CHUNK_SIZE,
            ) as stream:
                while self._running:
                    audio_chunk, _ = stream.read(CHUNK_SIZE)
                    audio_np = audio_chunk.flatten()
                    self._model.predict(audio_np)

                    # Check all wake word scores
                    for phrase, score in self._model.prediction_buffer.items():
                        latest_score = score[-1] if score else 0.0
                        if latest_score >= DETECTION_THRESHOLD:
                            now = time.monotonic()
                            if now - self._last_trigger >= COOLDOWN_SECONDS:
                                self._last_trigger = now
                                persona = WAKE_WORDS.get(phrase, "jarvis")
                                log.info(
                                    f"[wake_word] Detected '{phrase}' "
                                    f"(conf={latest_score:.2f}) → persona={persona}"
                                )
                                if self._callback:
                                    self._callback(persona, float(latest_score))

        except Exception as e:
            log.error(f"[wake_word] Listen loop crashed: {e}")
            self._running = False

    @property
    def available(self) -> bool:
        return self._ready

    @property
    def running(self) -> bool:
        return self._running


# ── Singleton ─────────────────────────────────────────────────────────────────
_detector: WakeWordDetector | None = None

def get_wake_word_detector() -> WakeWordDetector:
    global _detector
    if _detector is None:
        _detector = WakeWordDetector()
    return _detector
