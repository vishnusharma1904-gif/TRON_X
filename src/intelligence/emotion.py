"""
TRON-X Emotion Detection Engine  (Phase 1)
--------------------------------------------
Detects user emotional state from text patterns, punctuation cues,
vocabulary, and conversation rhythm.

EmotionState is injected into the persona system prompt so Jarvis/Friday
can respond with contextually appropriate empathy and energy.

Detected states:
  neutral     — baseline, no strong signal
  frustrated  — caps, anger words, repeated punctuation, debugging rage
  excited     — enthusiasm words, multiple !, positivity
  confused    — multiple ?, rambling, contradiction signals
  tired       — low energy words, minimal punctuation, brevity
  playful     — jokes, emojis, banter cues, sarcasm markers
  sad         — sadness words, loneliness, self-doubt
  stressed    — deadline/overwhelm language, urgency markers
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Emotion(str, Enum):
    NEUTRAL    = "neutral"
    FRUSTRATED = "frustrated"
    EXCITED    = "excited"
    CONFUSED   = "confused"
    TIRED      = "tired"
    PLAYFUL    = "playful"
    SAD        = "sad"
    STRESSED   = "stressed"


@dataclass
class EmotionState:
    primary:   Emotion       = Emotion.NEUTRAL
    intensity: float         = 0.0          # 0.0 (weak) – 1.0 (strong)
    secondary: Emotion | None = None
    cues:      list[str]     = field(default_factory=list)
    raw_scores: dict[str, float] = field(default_factory=dict)

    @property
    def is_neutral(self) -> bool:
        return self.primary == Emotion.NEUTRAL or self.intensity < 0.25

    def persona_hint(self) -> str:
        """Human-readable hint injected into system prompts."""
        if self.is_neutral:
            return ""
        intensity_word = (
            "slightly" if self.intensity < 0.4 else
            "noticeably" if self.intensity < 0.65 else
            "strongly"
        )
        secondary_note = f" (with a hint of {self.secondary.value})" if self.secondary else ""
        return (
            f"[EMOTION SIGNAL] The user appears {intensity_word} {self.primary.value}"
            f"{secondary_note}. Detected cues: {', '.join(self.cues[:4]) if self.cues else 'none'}."
        )


# ---------------------------------------------------------------------------
# Pattern libraries
# ---------------------------------------------------------------------------

_FRUSTRATED_WORDS: list[str] = [
    "ugh", "uggh", "argh", "wtf", "wth", "damn", "dammit", "ffs", "fml",
    "why isn't", "why doesn't", "why won't", "not working", "doesn't work",
    "broken", "garbage", "trash", "useless", "stupid", "hate this",
    "this sucks", "so annoying", "ugh why", "kill me", "fix this",
    "still broken", "still not", "nothing works", "cant believe",
    "why is this", "what the hell", "what the heck", "seriously",
    "bruh", "bro seriously", "bro why",
]

_EXCITED_WORDS: list[str] = [
    "amazing", "awesome", "wow", "great", "fantastic", "incredible",
    "love it", "love this", "finally", "it works", "yes!", "yess", "yes!!",
    "let's go", "lets go", "fire", "lit", "sick", "perfect", "nailed it",
    "genius", "brilliant", "beautiful", "this is insane", "holy moly",
    "holy cow", "omg", "omfg", "can't believe", "unbelievable",
    "banger", "slaps", "top tier", "goated",
]

_CONFUSED_WORDS: list[str] = [
    "confused", "don't understand", "dont understand", "not sure",
    "what does that mean", "how does this", "can you explain",
    "i don't get", "i dont get", "makes no sense", "what exactly",
    "huh?", "huh", "what?", "wait what", "i'm lost", "im lost",
    "clarify", "what do you mean", "help me understand", "still confused",
    "elaborate", "not following", "lost me",
]

_TIRED_WORDS: list[str] = [
    "tired", "exhausted", "sleepy", "can't sleep", "no sleep",
    "been at this", "hours straight", "all night", "all day",
    "burned out", "burn out", "drained", "just want to", "whatever",
    "idk man", "idc", "doesn't matter", "nevermind", "forget it",
    "too tired", "too exhausted", "give up", "done for the day",
]

_PLAYFUL_WORDS: list[str] = [
    "lol", "lmao", "lmfao", "haha", "hehe", "😂", "🤣", "😎", "🔥",
    "jokes", "kidding", "jk", "ngl", "tbh", "fr fr", "no cap",
    "lowkey", "highkey", "vibe", "vibes", "slay", "bestie",
    "banter", "roast me", "fight me", "change my mind", "controversial",
    "hot take", "unpopular opinion",
]

_SAD_WORDS: list[str] = [
    "sad", "depressed", "depression", "unhappy", "lonely", "alone",
    "nobody", "no one", "miss you", "i miss", "heartbroken",
    "worthless", "hopeless", "crying", "cried", "hurt", "pain",
    "struggling", "hard time", "rough time", "not okay", "not ok",
    "feel like", "overwhelmed", "losing it", "breaking down",
    "not well", "not doing well",
]

_STRESSED_WORDS: list[str] = [
    "deadline", "urgent", "asap", "need this now", "hurry",
    "running out of time", "panic", "panicking", "stressed",
    "pressure", "exam tomorrow", "due tomorrow", "due today",
    "last minute", "emergency", "critical", "production down",
    "server down", "boss is", "client is", "ship it", "release",
    "no time", "help fast", "quick", "quickly", "need asap",
]

# Each pattern bank: (word_list, Emotion, weight_per_hit)
_BANKS: list[tuple[list[str], Emotion, float]] = [
    (_FRUSTRATED_WORDS, Emotion.FRUSTRATED, 0.30),
    (_EXCITED_WORDS,    Emotion.EXCITED,    0.25),
    (_CONFUSED_WORDS,   Emotion.CONFUSED,   0.25),
    (_TIRED_WORDS,      Emotion.TIRED,      0.25),
    (_PLAYFUL_WORDS,    Emotion.PLAYFUL,    0.20),
    (_SAD_WORDS,        Emotion.SAD,        0.35),
    (_STRESSED_WORDS,   Emotion.STRESSED,   0.30),
]

# ---------------------------------------------------------------------------
# Punctuation / style signals
# ---------------------------------------------------------------------------

_CAPS_RATIO_THRESHOLD  = 0.35   # >35% caps chars → frustration/excitement signal
_EXCLAIM_THRESHOLD     = 3      # 3+ ! in a message
_QUESTION_THRESHOLD    = 3      # 3+ ? in a message
_ELLIPSIS_THRESHOLD    = 2      # 2+ ... in a message (tired/sad)
_VERY_SHORT_THRESHOLD  = 12     # chars; very short messages can signal tiredness

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002600-\U000027BF"  # misc symbols
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "]+",
    flags=re.UNICODE,
)


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

class EmotionDetector:
    """
    Lightweight, no-external-model emotion detector.
    Purely pattern-based — fast enough for every request.
    """

    def detect(self, text: str) -> EmotionState:
        if not text or not text.strip():
            return EmotionState()

        text_lower  = text.lower()
        scores: dict[str, float] = {}
        cues:   list[str]        = []

        # ── 1. Word/phrase matching ────────────────────────────────────────────
        for word_list, emotion, weight in _BANKS:
            hits = [w for w in word_list if w in text_lower]
            if hits:
                score = min(1.0, len(hits) * weight)
                scores[emotion.value] = scores.get(emotion.value, 0.0) + score
                cues.extend(hits[:2])  # keep top 2 cues per bank

        # ── 2. Punctuation signals ─────────────────────────────────────────────
        exclaim_count  = text.count("!")
        question_count = text.count("?")
        ellipsis_count = text.count("…") + text.count("...")

        # All-caps frustration/excitement signal
        alpha_chars = [c for c in text if c.isalpha()]
        if alpha_chars:
            caps_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
            if caps_ratio > _CAPS_RATIO_THRESHOLD and len(text) > 8:
                # Distinguish from excitement: caps with anger words → frustrated
                if any(w in text_lower for w in ["fix", "broken", "why", "not"]):
                    scores["frustrated"] = scores.get("frustrated", 0.0) + 0.3
                    cues.append("ALL_CAPS")
                else:
                    scores["excited"] = scores.get("excited", 0.0) + 0.25
                    cues.append("ALL_CAPS")

        if exclaim_count >= _EXCLAIM_THRESHOLD:
            scores["excited"]    = scores.get("excited",    0.0) + 0.25
            scores["frustrated"] = scores.get("frustrated", 0.0) + 0.15
            cues.append(f"{exclaim_count}x!")

        if question_count >= _QUESTION_THRESHOLD:
            scores["confused"] = scores.get("confused", 0.0) + 0.30
            cues.append(f"{question_count}x?")

        if ellipsis_count >= _ELLIPSIS_THRESHOLD:
            scores["tired"] = scores.get("tired", 0.0) + 0.25
            scores["sad"]   = scores.get("sad",   0.0) + 0.15
            cues.append("ellipsis_pattern")

        # Emoji presence → playful
        if _EMOJI_PATTERN.search(text):
            scores["playful"] = scores.get("playful", 0.0) + 0.20
            cues.append("emoji_detected")

        # Very short, no punctuation → could be tired/terse
        clean_len = len(text.strip())
        if clean_len <= _VERY_SHORT_THRESHOLD and exclaim_count == 0:
            scores["tired"] = scores.get("tired", 0.0) + 0.15

        # ── 3. Pick winner ─────────────────────────────────────────────────────
        if not scores:
            return EmotionState(raw_scores=scores)

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_emotion_name, top_score = sorted_scores[0]

        # Clamp intensity to [0, 1]
        intensity = min(1.0, top_score)

        primary   = Emotion(top_emotion_name)
        secondary = None

        if len(sorted_scores) > 1 and sorted_scores[1][1] >= 0.20:
            secondary = Emotion(sorted_scores[1][0])

        # Deduplicate cues
        seen = set()
        unique_cues = []
        for c in cues:
            if c not in seen:
                seen.add(c)
                unique_cues.append(c)

        return EmotionState(
            primary=primary,
            intensity=round(intensity, 2),
            secondary=secondary,
            cues=unique_cues,
            raw_scores={k: round(v, 3) for k, v in scores.items()},
        )


# ── Singleton ─────────────────────────────────────────────────────────────────

_detector: EmotionDetector | None = None


def get_emotion_detector() -> EmotionDetector:
    global _detector
    if _detector is None:
        _detector = EmotionDetector()
    return _detector


def detect_emotion(text: str) -> EmotionState:
    """Convenience function — runs emotion detection on a text snippet."""
    return get_emotion_detector().detect(text)
