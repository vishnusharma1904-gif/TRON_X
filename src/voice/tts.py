"""
TRON-X Text-to-Speech Engine
Phase 14: ElevenLabs + sentence-stream TTS

Provider chain:
  1. ElevenLabs  -- cloud, highest quality, ~200ms TTFA (if API key set)
  2. Kokoro-82M  -- local CPU, near-ElevenLabs quality
  3. edge-tts    -- Microsoft Azure neural voices (online)
  4. pyttsx3     -- offline Windows SAPI (no internet needed)

New in Phase 14:
  - synthesize_stream(): async generator for sentence-level streaming TTS.
    Accepts an async text stream (e.g. from LLM), splits on sentence
    boundaries, and yields audio chunks as each sentence is ready.
    TTFA drops from (full LLM response time) to (time to first sentence ~1-2s).
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import time
from typing import AsyncIterable, AsyncGenerator, Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------
# Kokoro note: the bundled models/voices-v1.1-zh.bin pack ships only THREE
# English voices -- af_maple, af_sol, bf_vale -- and ALL THREE ARE FEMALE
# (Kokoro naming: a/b = American/British accent, f/m = female/male; there is
# no "*m" English voice in this pack -- "bm_george" etc. do NOT exist here).
# Every "male" persona (JARVIS, STARK, THOR, HULK, ...) was previously either
# pinned to "af_maple" (a female voice) or to a nonexistent male voice id,
# which throws inside kokoro_onnx and disables Kokoro for the whole session.
# Fix:
#   - "kokoro_voice": None for male personas -> synthesize() skips Kokoro for
#     them entirely and goes to ElevenLabs (if configured) or a male
#     pyttsx3/SAPI voice instead.
#   - Each of the 21 A.V.E.N.G.E.R.S personas gets its own resolved profile
#     (via get_voice_profile()) so ElevenLabs, when configured, gives
#     genuinely distinct, gender-correct voices across the roster.
VOICE_PROFILES = {
    "jarvis": {
        "gender":           "male",
        "kokoro_voice":     None,      # no English male voice in this pack
        "edge_voice":       "en-GB-RyanNeural",
        "speed":            0.92,
        "lang":             "en-gb",
        "elevenlabs_voice": None,   # filled at runtime from settings
    },
    "friday": {
        "gender":           "female",
        "kokoro_voice":     "af_sol",
        "edge_voice":       "en-US-JennyNeural",
        "speed":            1.0,
        "lang":             "en-us",
        "elevenlabs_voice": None,   # filled at runtime from settings
    },
}

# ElevenLabs "Premade Voices" library -- used to give the 21 A.V.E.N.G.E.R.S
# personas distinct, gender-correct voices when ELEVENLABS_API_KEY is set.
_EL_MALE = {
    "adam":   "pNInz6obpgDQGcFmaJgB",
    "antoni": "ErXwobaYiN019PkySvjV",
    "arnold": "VR6AewLTigWG4xSOukaG",
    "josh":   "TxGEqnHWrfWFTfGW9XjX",
    "sam":    "yoZ06aMxZJJ28mfd3POQ",
}
_EL_FEMALE = {
    "rachel": "21m00Tcm4TlvDq8ikWAM",
    "domi":   "AZnzlk1XvdvUeBnXmlld",
    "bella":  "EXAVITQu4vr4xnSDxMaL",
    "elli":   "MF3mGyEYCl7XYWbV9V6O",
}

# Per-persona overrides layered on top of the gender-matched "jarvis"
# (male) / "friday" (female) base profile above. Male personas deliberately
# omit "kokoro_voice" so they inherit None (no Kokoro -- see note above).
# Female personas get one of Kokoro's 3 real English voices for variety.
AVENGER_VOICE_OVERRIDES: dict[str, dict] = {
    "jarvis":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["adam"]},
    "friday":   {"gender": "female", "elevenlabs_voice": _EL_FEMALE["rachel"], "kokoro_voice": "af_sol"},
    "oracle":   {"gender": "female", "elevenlabs_voice": _EL_FEMALE["domi"],   "kokoro_voice": "af_maple"},
    "athena":   {"gender": "female", "elevenlabs_voice": _EL_FEMALE["bella"],  "kokoro_voice": "bf_vale"},
    "zeus":     {"gender": "male",   "elevenlabs_voice": _EL_MALE["arnold"]},
    "stark":    {"gender": "male",   "elevenlabs_voice": _EL_MALE["antoni"]},
    "steve":    {"gender": "male",   "elevenlabs_voice": _EL_MALE["adam"]},
    "herald":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["josh"],   "edge_voice": "en-GB-RyanNeural", "lang": "en-gb"},
    "vision":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["antoni"], "edge_voice": "en-GB-RyanNeural", "lang": "en-gb"},
    "banner":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["sam"]},
    "ultron":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["arnold"]},
    "thor":     {"gender": "male",   "elevenlabs_voice": _EL_MALE["arnold"]},
    "atlas":    {"gender": "male",   "elevenlabs_voice": _EL_MALE["adam"]},
    "hercules": {"gender": "male",   "elevenlabs_voice": _EL_MALE["josh"]},
    "strange":  {"gender": "male",   "elevenlabs_voice": _EL_MALE["antoni"]},
    "spectre":  {"gender": "male",   "elevenlabs_voice": _EL_MALE["sam"]},
    "jalen":    {"gender": "male",   "elevenlabs_voice": _EL_MALE["josh"]},
    "ants":     {"gender": "male",   "elevenlabs_voice": _EL_MALE["adam"]},
    "jerome":   {"gender": "male",   "elevenlabs_voice": _EL_MALE["antoni"]},
    "hulk":     {"gender": "male",   "elevenlabs_voice": _EL_MALE["sam"]},
    "pepper":   {"gender": "female", "elevenlabs_voice": _EL_FEMALE["elli"], "kokoro_voice": "af_sol"},
}


def get_voice_profile(persona: str) -> dict:
    """Resolve the full synthesis profile for any of the 21 A.V.E.N.G.E.R.S
    persona ids (or the base "jarvis"/"friday" ids).

    Starts from the gender-matched base profile (male -> "jarvis",
    female -> "friday") so every persona inherits sane defaults
    (kokoro_voice=None for male, a real Kokoro voice for female), then
    layers the persona-specific overrides (ElevenLabs voice id, accent,
    etc.) on top. Unknown persona ids fall back to the "jarvis" (male)
    profile.
    """
    override = AVENGER_VOICE_OVERRIDES.get(persona, {})
    gender = override.get("gender", "male")
    base = VOICE_PROFILES["friday"] if gender == "female" else VOICE_PROFILES["jarvis"]
    profile = dict(base)
    profile.update(override)
    return profile

# ---------------------------------------------------------------------------
# Sentence splitting helpers
# ---------------------------------------------------------------------------
# Splits on '. ', '! ', '? ' — but not on common abbreviations
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')
_ABBREV = re.compile(
    r'\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|i\.e|e\.g|Fig|No|Vol|p)\.\s*$',
    re.IGNORECASE,
)
_MIN_SENTENCE_CHARS = 8   # don't synthesize micro-fragments

_PROSODY_MARKERS = re.compile(r"\[(pause|slow|fast|whisper|normal)\]", re.IGNORECASE)

# Phase 38: markdown/LaTeX/emoji → speakable text lives in its own module
from src.voice.speech_text import preprocess_for_speech as _preprocess_text  # noqa: E402


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, respecting abbreviations."""
    parts = _SENTENCE_END.split(text)
    sentences: list[str] = []
    buf = ""
    for part in parts:
        combined = (buf + " " + part).strip() if buf else part.strip()
        if _ABBREV.search(combined):
            buf = combined   # abbreviation — keep accumulating
        else:
            if len(combined) >= _MIN_SENTENCE_CHARS:
                sentences.append(combined)
            buf = ""
    if buf.strip() and len(buf.strip()) >= _MIN_SENTENCE_CHARS:
        sentences.append(buf.strip())
    return sentences or [text.strip()]


# ---------------------------------------------------------------------------
# TTSEngine
# ---------------------------------------------------------------------------

class TTSEngine:
    def __init__(self):
        self._kokoro = None
        self._kokoro_available = False
        self._el_client = None          # ElevenLabs async client
        self._el_available = False
        self._init_elevenlabs()
        self._init_kokoro()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init_elevenlabs(self) -> None:
        if not settings.elevenlabs_api_key:
            log.info("[tts] No ELEVENLABS_API_KEY — ElevenLabs disabled")
            return
        # Use direct HTTP (httpx) — no SDK required
        self._el_available = True
        log.info("[tts] ElevenLabs ready via HTTP (model=%s)", settings.elevenlabs_model)

    def _init_kokoro(self) -> None:
        try:
            from kokoro_onnx import Kokoro
            model_path  = "models/kokoro-v1.1-zh.onnx"
            voices_path = "models/voices-v1.1-zh.bin"
            if not (os.path.exists(model_path) and os.path.exists(voices_path)):
                log.info(
                    "[tts] Kokoro model files not found at models/ — "
                    "falling back to edge-tts. "
                    "Download from: https://github.com/thewh1teagle/kokoro-onnx/releases"
                )
                return
            # v1.1-zh ships its own vocab in models/config.json. English works
            # on the default vocab too, but the release config is the correct
            # one for this model (and required for its Mandarin voices), so we
            # load it when present and warn when it isn't.
            vocab_config = "models/config.json" if os.path.exists("models/config.json") else None
            if vocab_config is None:
                log.warning(
                    "[tts] models/config.json missing — using default vocab "
                    "(English-only). Restore it from: "
                    "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/raw/main/config.json"
                )
            self._kokoro = Kokoro(model_path, voices_path, vocab_config=vocab_config)
            self._kokoro_available = True
            log.info("[tts] Kokoro v1.1-zh ready (local CPU, vocab=%s)",
                     "config.json" if vocab_config else "default/en")
        except ImportError:
            log.info("[tts] kokoro-onnx not installed — using edge-tts fallback")
        except Exception as e:
            log.warning("[tts] Kokoro init failed: %s — using edge-tts fallback", e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        persona: str = "jarvis",
        speed_override: Optional[float] = None,
        return_format: str = "wav",
        lang_hint: Optional[str] = None,
    ) -> dict:
        """
        Synthesize text to audio bytes.

        Returns:
            {
                "audio_bytes": bytes,
                "format":      str,       # "mp3" | "wav"
                "provider":    str,
                "latency_ms":  int,
                "char_count":  int,
            }
        """
        t0 = time.monotonic()
        clean_text = _preprocess_text(text)

        if not clean_text.strip():
            return {"audio_bytes": b"", "format": return_format,
                    "provider": "none", "latency_ms": 0, "char_count": 0}

        profile = get_voice_profile(persona)
        speed   = speed_override or profile["speed"]
        is_en   = (lang_hint or "en").lower().startswith("en")

        if profile.get("kokoro_voice") and self._kokoro_available and is_en:
            # Only personas with a real (female) Kokoro voice take this path.
            result = await self._synthesize_kokoro(clean_text, profile, speed, lang_hint=lang_hint, persona=persona)
        elif self._el_available:
            result = await self._synthesize_elevenlabs(clean_text, persona, profile, lang_hint=lang_hint)
        elif profile.get("gender") == "male" and is_en:
            # No Kokoro voice + no ElevenLabs key: use a male SAPI/pyttsx3
            # voice instead of falling through to a generic/female gTTS voice.
            result = await self._synthesize_pyttsx3_first(clean_text, profile, lang_hint=lang_hint)
        else:
            result = await self._synthesize_edge(clean_text, profile, lang_hint=lang_hint)

        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["char_count"] = len(clean_text)
        log.info(
            "[tts] Synthesized %d chars in %dms via %s",
            len(clean_text), result["latency_ms"], result["provider"],
        )
        return result

    async def synthesize_stream(
        self,
        text_stream: AsyncIterable[str],
        persona: str = "jarvis",
        lang_hint: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Sentence-streaming TTS async generator.

        Consumes an async text stream (e.g. from an LLM), buffers tokens,
        splits on sentence boundaries, and yields an audio chunk dict for
        each sentence as soon as it's ready.

        Yields:
            {
                "audio_bytes": bytes,
                "format":      str,
                "provider":    str,
                "sentence":    str,
                "index":       int,
                "latency_ms":  int,
                "char_count":  int,
            }

        Usage:
            async for chunk in tts.synthesize_stream(llm_gen(), persona="jarvis"):
                yield_sse(chunk["audio_bytes"], chunk["sentence"], chunk["index"])
        """
        buf   = ""
        index = 0

        async for token in text_stream:
            buf += token
            
            # Handle markdown code blocks
            # If we have an opening ``` but not a closing one, keep buffering!
            if "```" in buf:
                parts = buf.split("```")
                # parts length is 1 if no ```, 2 if one ```, 3 if two ```, etc.
                if len(parts) >= 3:
                    # Replace the entire code block with a spoken placeholder
                    # parts[0] is before, parts[1] is inside, parts[2] is after
                    buf = parts[0] + " — [Code shown on screen] — " + "```".join(parts[2:])
                else:
                    # We are currently inside a code block, keep buffering until closing ```
                    continue

            # Scan for complete sentence boundaries
            while True:
                m = _SENTENCE_END.search(buf)
                if not m:
                    break
                sentence = buf[:m.start() + 1].strip()
                remainder = buf[m.end():]

                # Skip abbreviation false-positives — merge back
                if _ABBREV.search(sentence):
                    break

                buf = remainder

                if len(sentence) < _MIN_SENTENCE_CHARS:
                    continue

                result = await self.synthesize(sentence, persona=persona, lang_hint=lang_hint)
                result["sentence"] = sentence
                result["index"]    = index
                index += 1
                yield result

        # Flush any remaining text
        tail = buf.strip()
        if tail and len(tail) >= 3:
            result = await self.synthesize(tail, persona=persona, lang_hint=lang_hint)
            result["sentence"] = tail
            result["index"]    = index
            yield result

    # ------------------------------------------------------------------
    # ElevenLabs
    # ------------------------------------------------------------------

    async def _synthesize_elevenlabs(self, text: str, persona: str, profile: Optional[dict] = None,
                                      lang_hint: Optional[str] = None) -> dict:
        profile = profile or get_voice_profile(persona)
        if persona == "jarvis":
            voice_id = settings.elevenlabs_voice_jarvis or profile.get("elevenlabs_voice")
        elif persona == "friday":
            voice_id = settings.elevenlabs_voice_friday or profile.get("elevenlabs_voice")
        else:
            # Any of the other 20 A.V.E.N.G.E.R.S personas: use their own
            # ElevenLabs voice id, falling back to the gender-matched
            # jarvis/friday default if one wasn't configured.
            voice_id = profile.get("elevenlabs_voice") or (
                settings.elevenlabs_voice_friday if profile.get("gender") == "female"
                else settings.elevenlabs_voice_jarvis
            )
        try:
            import httpx
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            payload = {
                "text": text,
                "model_id": settings.elevenlabs_model or "eleven_turbo_v2_5",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True,
                },
            }
            headers = {
                "xi-api-key": settings.elevenlabs_api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload, headers=headers,
                                         params={"output_format": "mp3_44100_128"})
                resp.raise_for_status()
                audio_bytes = resp.content

            if len(audio_bytes) > 200:
                return {
                    "audio_bytes": audio_bytes,
                    "format": "mp3",
                    "provider": "elevenlabs",
                    "language": lang_hint or "en",
                }
            raise ValueError(f"ElevenLabs returned empty audio ({len(audio_bytes)} bytes)")

        except Exception as e:
            log.warning("[tts] ElevenLabs failed: %s — falling back to next provider", e)
            if self._kokoro_available and profile.get("kokoro_voice"):
                return await self._synthesize_kokoro(text, profile, profile["speed"], lang_hint=lang_hint, persona=persona)
            if profile.get("gender") == "male":
                return await self._synthesize_pyttsx3_first(text, profile, lang_hint=lang_hint)
            return await self._synthesize_gtts(text, profile, lang_hint=lang_hint)

    # ------------------------------------------------------------------
    # Kokoro
    # ------------------------------------------------------------------

    async def _synthesize_kokoro(
        self,
        text: str,
        profile: dict,
        speed: float,
        lang_hint: Optional[str] = None,
        persona: str = "jarvis",
    ) -> dict:
        # Guard: Kokoro's ONNX model crashes on very short or purely-symbol inputs
        clean = text.strip()
        if len(clean) < 4 or not re.search(r'[a-zA-Z0-9]', clean):
            if self._el_available:
                return await self._synthesize_elevenlabs(text, persona, profile, lang_hint=lang_hint)
            if profile.get("gender") == "male":
                return await self._synthesize_pyttsx3_first(text, profile, lang_hint=lang_hint)
            return await self._synthesize_gtts(text, profile, lang_hint=lang_hint)

        # ROOT-CAUSE FIX (verified): the kokoro-v1.1-zh ONNX export raises
        # RuntimeException ("Expand node ... tensor shape.Size() must be >= 0")
        # for ANY speed < 1.0 — this is what crashed every JARVIS synthesis
        # (profile speed 0.92) and silently disabled Kokoro each session.
        # Speeds > 1.0 are also a no-op in this export (output is identical),
        # so we pin speed to exactly 1.0 for Kokoro. Persona pacing still
        # applies on the edge-tts / ElevenLabs paths, which honour speed.
        speed = 1.0

        loop = asyncio.get_event_loop()

        def _run():
            # Suppress ONNX C++ stderr spam around the call
            import os as _os
            old_stderr_fd = _os.dup(2)
            devnull_fd    = _os.open(_os.devnull, _os.O_WRONLY)
            _os.dup2(devnull_fd, 2)
            try:
                samples, sample_rate = self._kokoro.create(
                    clean,
                    voice=profile["kokoro_voice"],
                    speed=speed,
                    lang=profile["lang"],
                )
            finally:
                _os.dup2(old_stderr_fd, 2)
                _os.close(devnull_fd)
                _os.close(old_stderr_fd)

            import soundfile as sf
            buf = io.BytesIO()
            sf.write(buf, samples, sample_rate, format="WAV")
            return buf.getvalue()

        try:
            audio_bytes = await loop.run_in_executor(None, _run)
            if audio_bytes and len(audio_bytes) > 200:
                return {
                    "audio_bytes": audio_bytes,
                    "format": "wav",
                    "provider": "kokoro",
                    "language": lang_hint or profile.get("lang", "en"),
                }
            raise ValueError("Kokoro returned empty audio")
        except Exception as e:
            # Disable Kokoro for the rest of this session to stop ONNX spam
            log.warning("[tts] Kokoro failed (%s) — disabling for this session, using fallback", type(e).__name__)
            self._kokoro_available = False
            if self._el_available:
                return await self._synthesize_elevenlabs(text, persona, profile, lang_hint=lang_hint)
            if profile.get("gender") == "male":
                return await self._synthesize_pyttsx3_first(text, profile, lang_hint=lang_hint)
            return await self._synthesize_gtts(text, profile, lang_hint=lang_hint)

    # ------------------------------------------------------------------
    # edge-tts
    # ------------------------------------------------------------------

    async def _synthesize_edge(self, text: str, profile: dict, lang_hint: Optional[str] = None) -> dict:
        # edge-tts consistently returns 403 from Microsoft's endpoint.
        # Skip straight to gTTS to avoid noisy warnings on every request.
        return await self._synthesize_gtts(text, profile, lang_hint=lang_hint)

    # ------------------------------------------------------------------
    # gTTS (Google Text-to-Speech — free, no API key)
    # ------------------------------------------------------------------

    async def _synthesize_gtts(self, text: str, profile: dict, lang_hint: Optional[str] = None) -> dict:
        try:
            from gtts import gTTS
            import io as _io
            loop = asyncio.get_event_loop()
            hint = (lang_hint or "").lower()
            if hint.startswith("te"):
                # Native Telugu — gTTS reads Telugu script directly
                lang, tld = "te", "com"
            elif hint in ("en-in", "tenglish"):
                # Tenglish / romanised Telugu / Hyderabadi — Indian-English
                # voice pronounces code-mixed Latin-script Telugu far more
                # naturally than UK/US voices
                lang, tld = "en", "co.in"
            else:
                lang = "en"
                tld  = "co.uk" if "gb" in profile.get("lang", "en-us").lower() else "com"

            def _run():
                tts = gTTS(text=text, lang=lang, tld=tld, slow=False)
                buf = _io.BytesIO()
                tts.write_to_fp(buf)
                return buf.getvalue()

            audio_bytes = await loop.run_in_executor(None, _run)
            if len(audio_bytes) > 200:
                return {"audio_bytes": audio_bytes, "format": "mp3", "provider": "gtts", "language": lang}
            raise Exception("gTTS returned empty audio")
        except ImportError:
            log.warning("[tts] gTTS not installed — run: pip install gTTS")
        except Exception as e:
            log.warning("[tts] gTTS failed: %s — falling back to pyttsx3", e)

        return await self._synthesize_pyttsx3(text, profile, lang_hint=lang_hint)

    # ------------------------------------------------------------------
    # pyttsx3 (offline Windows SAPI)
    # ------------------------------------------------------------------

    async def _synthesize_pyttsx3(self, text: str, profile: dict, lang_hint: Optional[str] = None) -> dict:
        try:
            import pyttsx3
            loop         = asyncio.get_event_loop()
            is_british   = "gb" in profile.get("lang", "en-us").lower()
            gender       = profile.get("gender", "male")
            speed_factor = profile.get("speed", 1.0)

            def _run() -> bytes:
                engine    = pyttsx3.init()
                base_rate = engine.getProperty("rate") or 200
                engine.setProperty("rate", int(base_rate * speed_factor))

                voices = engine.getProperty("voices") or []
                if gender == "female":
                    preferred = (["hazel", "susan"] if is_british else []) + \
                                ["zira", "jenny", "aria", "samantha", "victoria", "susan", "hazel"]
                else:
                    preferred = (["george", "ryan"] if is_british else []) + \
                                ["david", "guy", "mark", "tony", "fred", "george", "ryan"]
                for want in preferred:
                    for v in voices:
                        if want in (v.name or "").lower():
                            engine.setProperty("voice", v.id)
                            break
                    else:
                        continue
                    break

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp = f.name
                engine.save_to_file(text, tmp)
                engine.runAndWait()
                try:
                    with open(tmp, "rb") as fh:
                        return fh.read()
                finally:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass

            audio_bytes = await loop.run_in_executor(None, _run)
            if audio_bytes and len(audio_bytes) > 100:
                log.info("[tts] pyttsx3 synthesis OK")
                return {
                    "audio_bytes": audio_bytes,
                    "format": "wav",
                    "provider": "pyttsx3",
                    "language": lang_hint or profile.get("lang", "en"),
                }
            raise Exception("pyttsx3 produced empty file")

        except ImportError:
            log.error("[tts] pyttsx3 not installed — run: pip install pyttsx3")
            return {"audio_bytes": b"", "format": "wav", "provider": "error"}
        except Exception as e:
            log.error("[tts] pyttsx3 failed: %s", e)
            return {"audio_bytes": b"", "format": "wav", "provider": "error"}

    async def _synthesize_pyttsx3_first(self, text: str, profile: dict, lang_hint: Optional[str] = None) -> dict:
        """For male personas with no Kokoro voice and no ElevenLabs key:
        try the local SAPI/pyttsx3 male voice first (gender-correct),
        falling back to gTTS only if pyttsx3 is unavailable or fails."""
        result = await self._synthesize_pyttsx3(text, profile, lang_hint=lang_hint)
        if result.get("audio_bytes"):
            return result
        log.info("[tts] pyttsx3 unavailable/failed for male voice — falling back to gTTS")
        return await self._synthesize_gtts(text, profile, lang_hint=lang_hint)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def provider(self) -> str:
        if self._el_available:
            return "elevenlabs"
        if self._kokoro_available:
            return "kokoro"
        return "gtts"

    @property
    def elevenlabs_available(self) -> bool:
        return self._el_available


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

async def list_elevenlabs_voices() -> list[dict]:
    """Return available ElevenLabs voices (requires API key)."""
    s = get_settings()
    if not s.elevenlabs_api_key:
        return []
    try:
        from elevenlabs.client import AsyncElevenLabs
        client = AsyncElevenLabs(api_key=s.elevenlabs_api_key)
        resp = await client.voices.get_all()
        return [
            {
                "voice_id":    v.voice_id,
                "name":        v.name,
                "category":    getattr(v, "category", ""),
                "description": getattr(v, "description", ""),
            }
            for v in resp.voices
        ]
    except Exception as e:
        log.warning("[tts] list_elevenlabs_voices failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_tts: TTSEngine | None = None


def get_tts() -> TTSEngine:
    global _tts
    if _tts is None:
        _tts = TTSEngine()
    return _tts
