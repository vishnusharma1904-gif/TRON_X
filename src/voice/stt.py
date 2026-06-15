"""
TRON-X Speech-to-Text
──────────────────────
Provider: Groq Whisper large-v3-turbo
  • Sub-200ms transcription on typical voice clips
  • Accepts raw audio bytes (webm, wav, mp3, ogg, m4a)
  • Returns transcript + detected language + confidence

Fallback: local faster-whisper (if Groq unavailable)
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

SUPPORTED_FORMATS = {".wav", ".mp3", ".webm", ".ogg", ".m4a", ".flac"}
WHISPER_MODEL = "whisper-large-v3-turbo"


class STTEngine:
    def __init__(self):
        self._groq_client = None
        self._local_model = None
        self._init_groq()

    def _init_groq(self) -> None:
        if not settings.groq_api_key:
            log.warning("[stt] No GROQ_API_KEY — STT will use local fallback only")
            return
        # Use direct HTTP (httpx) — no groq SDK required
        self._groq_client = True   # sentinel: key is present
        log.info("[stt] Groq Whisper ready via HTTP ✓")

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> dict:
        """
        Transcribe audio bytes to text.

        Returns:
            {
                "text":     str,
                "language": str,
                "duration": float,   # audio duration in seconds
                "latency_ms": int,
                "provider": str,
            }
        """
        t0 = time.monotonic()

        if self._groq_client:
            result = await self._transcribe_groq(audio_bytes, filename, language, prompt)
        else:
            result = await self._transcribe_local(audio_bytes)

        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        log.info(
            f"[stt] Transcribed {len(audio_bytes)//1024}KB in {result['latency_ms']}ms "
            f"via {result['provider']}: \"{result['text'][:60]}...\""
            if len(result['text']) > 60 else
            f"[stt] Transcribed via {result['provider']}: \"{result['text']}\""
        )
        return result

    async def _transcribe_groq(
        self,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str],
        prompt: Optional[str],
    ) -> dict:
        """Direct HTTP call to Groq Whisper — no SDK required."""
        try:
            import httpx
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
            # Detect format from filename
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
            mime_map = {"wav":"audio/wav","webm":"audio/webm","mp3":"audio/mpeg",
                        "ogg":"audio/ogg","m4a":"audio/mp4","flac":"audio/flac"}
            mime = mime_map.get(ext, "audio/webm")
            files = {"file": (filename, audio_bytes, mime)}
            data  = {"model": WHISPER_MODEL, "response_format": "verbose_json"}
            if language:
                data["language"] = language
            if prompt:
                data["prompt"] = prompt

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, files=files, data=data)
                resp.raise_for_status()
                j = resp.json()

            return {
                "text":     j.get("text", "").strip(),
                "language": j.get("language", "en"),
                "duration": j.get("duration", 0.0),
                "provider": "groq",
            }
        except Exception as e:
            log.warning(f"[stt] Groq transcription failed: {e} — trying local")
            return await self._transcribe_local(audio_bytes)

    async def _transcribe_local(self, audio_bytes: bytes) -> dict:
        """Fallback: faster-whisper running on CPU."""
        try:
            import asyncio
            from faster_whisper import WhisperModel

            if self._local_model is None:
                log.info("[stt] Loading faster-whisper base model (CPU)…")
                self._local_model = WhisperModel("base", device="cpu", compute_type="int8")
                log.info("[stt] Local Whisper ready ✓")

            loop = asyncio.get_event_loop()

            def _run():
                import io as _io
                segments, info = self._local_model.transcribe(
                    _io.BytesIO(audio_bytes), beam_size=5
                )
                text = " ".join(s.text for s in segments).strip()
                return text, info.language, info.duration

            text, lang, dur = await loop.run_in_executor(None, _run)
            return {"text": text, "language": lang, "duration": dur, "provider": "local_whisper"}

        except ImportError:
            log.error("[stt] faster-whisper not installed — pip install faster-whisper")
            return {"text": "", "language": "en", "duration": 0.0, "provider": "none"}
        except Exception as e:
            log.error(f"[stt] Local transcription failed: {e}")
            return {"text": "", "language": "en", "duration": 0.0, "provider": "error"}


# ── Singleton ─────────────────────────────────────────────────────────────────
_stt: STTEngine | None = None

def get_stt() -> STTEngine:
    global _stt
    if _stt is None:
        _stt = STTEngine()
    return _stt
