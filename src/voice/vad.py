"""
TRON-X Voice Activity Detection
─────────────────────────────────
Silero VAD — detects speech boundaries in audio streams.
Used to:
  1. Clip silence from the start/end of voice recordings
  2. Detect when the user has finished speaking (push-to-talk end)
  3. Gate the STT call so we don't transcribe silence

CPU-only, ~1MB model, real-time capable.
"""
from __future__ import annotations

import io
from typing import Optional

from src.core.logger import log


class VADEngine:
    def __init__(self):
        self._model = None
        self._utils = None
        self._ready = False
        self._init()

    def _init(self) -> None:
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
                verbose=False,
            )
            self._model = model
            self._utils = utils
            self._ready = True
            log.info("[vad] Silero VAD ready ✓")
        except Exception as e:
            log.warning(f"[vad] Silero VAD unavailable: {e} — VAD disabled")

    def has_speech(self, audio_bytes: bytes, sample_rate: int = 16000) -> bool:
        """
        Returns True if speech is detected in the audio.
        Audio should be 16kHz mono WAV/raw PCM.
        """
        if not self._ready:
            return True  # Assume speech if VAD unavailable

        try:
            import torch
            import numpy as np

            # Parse WAV or treat as raw PCM
            try:
                import soundfile as sf
                samples, sr = sf.read(io.BytesIO(audio_bytes))
                if sr != 16000:
                    # Resample to 16kHz
                    import scipy.signal as signal
                    samples = signal.resample(samples, int(len(samples) * 16000 / sr))
            except Exception:
                # Treat as raw int16 PCM
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            if samples.ndim > 1:
                samples = samples.mean(axis=1)  # Stereo to mono

            tensor = torch.from_numpy(samples).float()
            speech_prob = self._model(tensor, 16000).item()
            return speech_prob > 0.5

        except Exception as e:
            log.debug(f"[vad] Check failed: {e}")
            return True

    def trim_silence(
        self,
        audio_bytes: bytes,
        threshold: float = 0.5,
        padding_ms: int = 200,
    ) -> bytes:
        """
        Trim leading/trailing silence from audio.
        Returns trimmed audio bytes (same format as input).
        """
        if not self._ready:
            return audio_bytes

        try:
            import torch
            import numpy as np
            import soundfile as sf

            samples, sr = sf.read(io.BytesIO(audio_bytes))
            if samples.ndim > 1:
                samples = samples.mean(axis=1)

            # Resample to 16kHz for VAD
            if sr != 16000:
                import scipy.signal as signal
                samples_16k = signal.resample(samples, int(len(samples) * 16000 / sr))
            else:
                samples_16k = samples

            tensor = torch.from_numpy(samples_16k.astype(np.float32))
            get_speech_ts = self._utils[0]

            speech_timestamps = get_speech_ts(
                tensor, self._model,
                threshold=threshold,
                sampling_rate=16000,
            )

            if not speech_timestamps:
                return audio_bytes  # No speech found — return as-is

            # Map back to original sample rate
            ratio = sr / 16000
            pad = int(padding_ms * sr / 1000)
            start = max(0, int(speech_timestamps[0]["start"] * ratio) - pad)
            end   = min(len(samples), int(speech_timestamps[-1]["end"] * ratio) + pad)

            trimmed = samples[start:end]
            buf = io.BytesIO()
            sf.write(buf, trimmed, sr, format="WAV")
            return buf.getvalue()

        except Exception as e:
            log.debug(f"[vad] Trim failed: {e}")
            return audio_bytes

    @property
    def available(self) -> bool:
        return self._ready


# ── Singleton ─────────────────────────────────────────────────────────────────
_vad: VADEngine | None = None

def get_vad() -> VADEngine:
    global _vad
    if _vad is None:
        _vad = VADEngine()
    return _vad
