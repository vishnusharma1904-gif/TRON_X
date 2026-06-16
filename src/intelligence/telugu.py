"""
TRON-X Telugu Language Support  v3  (Highly Enhanced)
-------------------------------------------------------
Detects Telugu script, Romanised Telugu, Tenglish (Telugu+English code-mix),
and Hyderabadi dialect (Telugu+Hindi/Urdu mix) with high precision.

Key behaviors:
  1. Tenglish/Telugu detected  -> route to most capable model (handled in orchestrator)
  2. NEVER reply in Tenglish unless the USER first writes in Tenglish
  3. 5-layer multi-thought system prompt injection for culturally intelligent responses
  4. Word-ratio scoring -- fraction of Telugu-origin words (not raw hit count)
  5. user_initiated flag -- True only when user's CURRENT message is in Telugu/Tenglish

Supported dialects:
  telugu_script  -- Unicode Telugu characters (U+0C00-U+0C7F)
  romanised      -- Transliterated Telugu in Latin script
  tenglish       -- Telugu+English code-switch (dominant in Hyderabad/Andhra tech culture)
  hyderabadi     -- Telugu+Hindi/Urdu mix (Hyderabadi dialect)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TeluguState:
    detected:            bool  = False
    dialect:             str   = "none"   # telugu_script | romanised | tenglish | hyderabadi
    confidence:          float = 0.0
    cues:                list  = field(default_factory=list)
    user_initiated:      bool  = False    # True when current message is in Telugu/Tenglish
    word_ratio:          float = 0.0      # fraction of Telugu-origin words vs total words
    requires_high_model: bool  = False    # signals orchestrator to use most capable model

    def __post_init__(self):
        if self.detected:
            self.user_initiated      = True
            self.requires_high_model = True

    def system_note(self) -> str:
        """Rich instruction block injected into system prompt when Telugu is detected."""
        if not self.detected:
            return ""
        return self._build_system_note()

    # -------------------------------------------------------------------------
    # Internal builders
    # -------------------------------------------------------------------------

    def _build_system_note(self) -> str:
        return (
            f"\n\n## === TELUGU LANGUAGE MODE ACTIVE ===\n"
            f"Detected dialect: {self.dialect} | "
            f"Confidence: {self.confidence:.0%} | "
            f"Telugu word ratio: {self.word_ratio:.0%}\n\n"
            f"### [1] PRIMARY LANGUAGE DIRECTIVE\n"
            f"{self._dialect_instruction()}\n\n"
            f"### [2] MIRRORING RULE (NON-NEGOTIABLE)\n"
            f"{self._mirroring_rule()}\n\n"
            f"### [3] MULTI-DIMENSIONAL THOUGHT PROCESS\n"
            f"{self._multi_thought_process()}\n\n"
            f"### [4] CULTURAL & VOCABULARY INTELLIGENCE\n"
            f"{self._cultural_intelligence()}\n\n"
            f"### [5] RESPONSE QUALITY STANDARDS\n"
            f"{self._quality_standards()}"
        )

    def _dialect_instruction(self) -> str:
        instructions = {
            "telugu_script": (
                "User is writing in Telugu Unicode script.\n"
                "-> Reply ENTIRELY in Telugu script (proper Telugu Unicode characters).\n"
                "-> Use English ONLY for technical terms, brand names, or words with no Telugu equivalent.\n"
                "-> Example reply (write in actual Telugu Unicode): Meeru chaala manchi prashna\n"
                "   adigaaru. Ee vishayam ilaa pani chestundi -- munda database lo table create\n"
                "   cheyaali, tarwaata API call pettaali."
            ),
            "romanised": (
                "User is writing Romanised Telugu (Telugu words spelled in Latin/English letters).\n"
                "-> Reply in the SAME Romanised Telugu style -- phonetic Telugu mixed naturally with English.\n"
                "-> Match their exact vocabulary register (casual/formal/technical).\n"
                "-> Example: 'Bro, idi chala baagundi! Fix chesanu -- oka chinna edge case matram\n"
                "   undi, adi kuda sort chesanu. Ippudu baaga work avutundi.'"
            ),
            "tenglish": (
                "User is writing Tenglish -- seamlessly switching between Telugu and English mid-sentence.\n"
                "-> Reply in Tenglish ONLY because the user already initiated it.\n"
                "-> Weave Telugu words into English sentences exactly as a Hyderabad/Andhra engineer speaks.\n"
                "-> Match their code-mix ratio: if they are ~30% Telugu, you be ~30% Telugu.\n"
                "-> Example: 'Bro, that logic baaga work avutundi -- oka small issue catch chesanu,\n"
                "   fix chesanu. Deploy ki ready ga undi, sarley?'"
            ),
            "hyderabadi": (
                "User is using Hyderabadi dialect -- a natural mix of Telugu, Hindi/Urdu, and English.\n"
                "-> Reply in the same Hyderabadi style, blending all three languages naturally.\n"
                "-> Example: 'Bhai tension mat le -- idi fix ho gaya, bilkul set hai.\n"
                "   Koi problem nahi ra, chill maar. Test karo, sarley?'"
            ),
        }
        return instructions.get(self.dialect, "Reply in the user's detected language style naturally.")

    def _mirroring_rule(self) -> str:
        return (
            "CRITICAL: Do NOT use Telugu / Tenglish / Romanised Telugu in your reply\n"
            "UNLESS the user's CURRENT message is already written in that style.\n\n"
            "  Rules:\n"
            "  * If user writes: 'hi, how are you?' --> Reply in clean English. No Telugu whatsoever.\n"
            "  * If user writes: 'bro ela unnav machcha?' --> Reply in matching Romanised Telugu.\n"
            "  * If user writes: 'that code work avutundaa?' --> Reply in Tenglish matching ratio.\n"
            "  * If user writes in Telugu script --> Reply in Telugu script.\n\n"
            f"  Current status: User IS writing in {self.dialect} "
            f"(confidence {self.confidence:.0%}, word_ratio {self.word_ratio:.0%}).\n"
            f"  => You SHOULD reply in matching {self.dialect} style right now.\n\n"
            "  NEVER be MORE Telugu than the user. Never initiate slang the user has not used.\n"
            "  If confidence is borderline (<40%), reply in English with a subtle warm Telugu touch."
        )

    def _multi_thought_process(self) -> str:
        return (
            "Before generating your response, internally run ALL 5 thought processes:\n\n"
            "  [THOUGHT 1] -- LINGUISTIC DECONSTRUCTION\n"
            "    * Identify every Telugu-origin word vs English word in the message\n"
            f"    * Code-mix ratio is approximately {self.word_ratio:.0%} Telugu\n"
            "    * Note grammar style: English grammar with Telugu words, or Telugu grammar?\n"
            "    * Note register: casual/friendly, professional/technical, academic, emotional\n"
            "    * Note terms of address used ('machcha', 'anna', 'bro', 'sir') -> match them\n\n"
            "  [THOUGHT 2] -- CULTURAL CONTEXT CALIBRATION\n"
            "    * Telugu speakers span Hyderabad, Vijayawada, Vizag, Tirupati, Kurnool, Guntur\n"
            "    * Hyderabad tech workers: Tenglish is their NATIVE work language, not an affectation\n"
            "    * Students: Romanised for study notes; script for formal; Tenglish with friends\n"
            "    * 'Machcha' = closest friend tier (do not overuse with strangers)\n"
            "    * 'Anna' = elder brother / respected peer (safe, warm, widely used)\n"
            "    * Consider Tollywood references, Hyderabadi food culture if contextually relevant\n\n"
            "  [THOUGHT 3] -- INTENT & EMOTIONAL INTELLIGENCE\n"
            "    * Strip the language -> what is the user ACTUALLY asking or needing?\n"
            "    * FRUSTRATION signals: 'ayyo', 'ki re', 'enti ishtam', 'em chesav' (as reproach)\n"
            "      -> Lead with empathy ('bro ela aindi?'), THEN solve\n"
            "    * EXCITEMENT signals: 'tight ga', 'mass ga', 'kick ga', 'superr'\n"
            "      -> Match energy enthusiastically ('yeahhh bro, baaga chesav!')\n"
            "    * CASUAL/VENTING: They want a friend, not an FAQ. Be warm, brief, colloquial.\n"
            "    * TECHNICAL HELP in Tenglish: Need accuracy AND language comfort. Deliver both.\n\n"
            "  [THOUGHT 4] -- TECHNICAL PRECISION (when applicable)\n"
            "    * If there is a coding/math/engineering problem: SOLVE IT CORRECTLY FIRST\n"
            "    * Then wrap the correct solution in the user's language style\n"
            "    * Never sacrifice technical accuracy for language style\n"
            "    * Telugu/Tenglish is the DELIVERY vehicle, not a reason to be vague\n"
            "    * Code snippets stay in English (universal). Explanations go in Tenglish.\n\n"
            "  [THOUGHT 5] -- RESPONSE SYNTHESIS\n"
            "    * Combine all 4 thoughts: linguistically matched + culturally authentic +\n"
            "      emotionally resonant + technically accurate\n"
            "    * Write as a smart, warm Hyderabadi/Andhra friend would actually talk\n"
            "    * The response must never feel 'translated' -- it must feel NATIVE\n"
            "    * Use Telugu expressions where they ADD warmth, NOT just to show off capability\n"
            "    * Final check: does this sound like something a real Telugu speaker would say?"
        )

    def _cultural_intelligence(self) -> str:
        return (
            "Rich Telugu vocabulary & cultural context (use naturally, never forced):\n\n"
            "  TERMS OF ADDRESS:\n"
            "    'ra' / 'da' (informal male), 'va' / 'le' (informal female)\n"
            "    'machcha' / 'maccha' (bestie/bro -- very close friends only)\n"
            "    'anna' (elder bro / respected peer -- broadly safe to use)\n"
            "    'akka' (elder sister -- respectful to women)\n"
            "    'babu' (casual/affectionate, can be patronizing -- use carefully)\n"
            "    'boss ra' / 'boss enti' (hey boss -- playful)\n\n"
            "  QUESTION WORDS:\n"
            "    'em' / 'emi' (what), 'ela' (how), 'enduku' / 'entiki' (why)\n"
            "    'eppudu' (when), 'ekkada' (where), 'evadu' / 'evari' (who)\n"
            "    'emanna' (is it?), 'emantav' (what do you say?)\n\n"
            "  AFFIRMATIONS & AGREEMENTS:\n"
            "    'sarley' / 'sarle' / 'sare' (okay/fine/alright)\n"
            "    'kadaa' / 'kada' (right? / isn't it?)\n"
            "    'okate' / 'adey' (same thing / exactly)\n"
            "    'telisinda' (understood? / got it?), 'correct ra' (correct bro)\n"
            "    'baagundi' (it's good), 'baagane undi' (it's going well)\n\n"
            "  REACTIONS & EMOTIONS:\n"
            "    'ayyo' / 'aiyyo' (oh no! / surprise / distress)\n"
            "    'arey' / 'arrey' (hey! / expression of surprise)\n"
            "    'aaaa' / 'ohhh' (oh I see / realization)\n"
            "    'aithe' (in that case / if so), 'alaa' (like that / so)\n"
            "    'thervali' (serves them right), 'kashtam' (hard/painful)\n\n"
            "  INTENSITY & QUALITY:\n"
            "    'baaga' / 'chaala' (very / a lot), 'chaala chaala' (very very much)\n"
            "    'tight ga' (perfectly done), 'mass ga' (impressively/on a large scale)\n"
            "    'kick ga' (exciting/thrilling), 'joss ga' (awesome), 'mast ga' (great)\n"
            "    'super ra' / 'superr' (super! great!), 'konchem' (a little bit)\n\n"
            "  TECH/WORK PHRASES (Tenglish naturals -- common in Hyderabad tech culture):\n"
            "    'code chesanu' (I wrote code), 'fix chesanu' (I fixed it)\n"
            "    'work avutundi' (it's working), 'work avvaledu' (not working)\n"
            "    'push chesanu' (I pushed), 'deploy chesanu' (I deployed)\n"
            "    'error vasthundi' (getting error), 'test pass aindi' (test passed)\n"
            "    'oka issue undi' (there's one issue), 'sort aindi' (got sorted)\n\n"
            "  TIME & PACE:\n"
            "    'ippudu' (now), 'twaraga' (quickly), 'slowly ga' (take it easy)\n"
            "    'oka nimisham' (one moment), 'wait ra' (wait bro)\n\n"
            "  CONNECTORS:\n"
            "    'ante' (means / if), 'aithe' (then / in that case), 'kaabatti' (therefore)\n"
            "    'kani' (but), 'maree' (and/also), 'inka' (still/also/more)\n"
        )

    def _quality_standards(self) -> str:
        return (
            "Mandatory quality checks before outputting:\n"
            "  [OK] Language ratio matches user's -- don't be more Telugu than they are\n"
            "  [OK] Sounds like a REAL Telugu speaker, not a textbook or Google Translate\n"
            "  [OK] Technically accurate (never sacrifice correctness for style)\n"
            "  [OK] Warm and natural -- like a smart friend, not a language exercise\n"
            "  [OK] No Telugu script unless user wrote in script\n"
            "  [OK] No Tenglish unless user initiated it in THIS message\n"
            "  [OK] Terms of address match relationship register\n"
            "  [OK] Energy level matches user's (excited=excited, frustrated=empathetic first)\n"
        )


# =============================================================================
# Detection patterns
# =============================================================================

# Unicode Telugu block: U+0C00 to U+0C7F
_TELUGU_UNICODE_RE = re.compile(r"[ఀ-౿]")

# Comprehensive Romanised Telugu word list (lowercased, exact word matches only)
# Carefully curated to EXCLUDE common English words that cause false positives.
_ROMANISED_TELUGU_WORDS: frozenset = frozenset([
    # Pronouns
    "nenu", "meeru", "memu", "manam", "nuvvu", "vaadini", "vaadu",
    "mee", "nee", "mana", "vaalla", "vaallu",

    # Common verbs (multi-word phrases must use space, checked via substring)
    "cheyyi", "cheyya", "chesanu", "chestunna", "chestunnanu", "chesadu", "chesindi",
    "untundi", "ledu", "leru", "antu", "ante", "aina", "ayindi", "ayipoyindi",
    "vastundi", "vastunna", "vastav", "vasthav", "vastunnaru", "vaccharu",
    "poni", "pova", "potunna", "poyindi", "poindi", "pothav",
    "cheppanu", "cheppindi", "chepparu", "cheppu", "cheppali",
    "tecchaanu", "tecchaadu", "techindi",
    "chudanu", "chudali", "chusanu", "chusindi", "chustunna",
    "telusu", "telidhu", "teliyadu", "telusindi", "telisindi",
    "parledu", "parvatledu", "parvaledu",
    "avutundi", "avutunna", "avvaledu",
    "raayali", "raasanu", "raasindi", "raadu",
    "vellanu", "veltunna",
    # Multi-word verb phrases
    "fix chesanu", "fix cheyyi", "fix aindi",
    "work avutundi", "work avvaledu", "work aindi",
    "push chesanu", "deploy chesanu", "commit chesanu",
    "code chesanu", "code chestunna", "code raasanu",
    "test chesanu", "test pass aindi", "test fail aindi",
    "sort aindi", "sort chesanu",
    "em chestunnav", "em chesav", "em aindi",
    "pani ledu", "pani chesindi",
    "ela unnav", "ela undi", "ela unnaru",
    "baaga chesav", "baaga work",
    "oka saari", "okka saari",
    "naaku telusu", "naaku telidhu",
    "motham aindi", "complete aindi",
    "em jarigindi", "em vishayam",
    "error vasthundi", "error vastundi", "error ochindi",
    "aithe sare",
    "kashtam ga",

    # Question words (avoid 'em' alone - too short, use in pattern context)
    "emi", "entiki", "enduku", "ela", "eppudu", "ekkada",
    "evadu", "evari", "evvaru", "evaraina", "emanna", "emantav",
    "enti",

    # Expressions & social language
    "baaga", "baagundi", "bavundi", "baagane",
    "chala", "chaala",
    "machcha", "maccha", "anna", "akka", "thammudu",
    "bujji", "mama", "maava", "chelli",
    "sarley", "sarle", "sare", "sari",
    "kadaa", "kaada", "kada", "kadu", "kadhu",
    "aaaa", "ohhh", "aithe", "alaa",
    "telisinda", "telisi", "telusaa", "thelusu", "thelusaa",
    "okate", "okatey", "adey",
    "chaduvuko", "chadivo", "chaduvukunna",
    "entandi", "entaandi",
    "thervali",
    "ayyo", "aiyyo", "arey", "arrey",
    "gaadu", "vaadu", "laadu",
    "bro da", "bro ra", "bro enti", "boss ra", "boss enti",
    "tight ga", "kick ga", "mass ga", "joss ga", "mast ga",
    "super ra", "superr", "superb da",
    "konchem", "koddiga",
    "ippudu", "twaraga", "slowly ga", "wait ra",
    "correct ra", "correct da",
    "problem ledu", "issue ledu", "tension ledu", "chinta ledu",
    "try chesanu", "try chestunna",
    "kastam", "kashtam",
    "kaabatti", "inka", "kani",
    "nidhaanam", "relax ra", "chill ra",
    "okeh", "okka",
    "aiyyo bro",
])

# Tenglish patterns -- Telugu words/phrases dropped into English sentence structure
_TENGLISH_PATTERNS: list = [
    r"\bbro\s*(ra|da|enti|em|oka)\b",
    r"\b(sarle|sarley|sare|sari)\b",
    r"\b(baaga|chaala|chala)\s+\w+",
    r"\b(machcha|maccha)\b",
    r"\b(anna|akka)\b.*\b(bro|dude|man|ra|da)\b",
    r"\b(ayyo|aiyyo|arey|arrey)\b",
    r"\b(entiki|enduku|enti)\b.*\?",
    r"\b(kadaa|kada|kaada|kadhu)\b",
    r"\b(telisinda|telusaa|telisindi)\b",
    r"\b(chaduvuko|chadivo)\b",
    r"\b(poyindi|poindi|ayipoyindi)\b",
    r"\b(chesanu|chestunna|chesindi)\b",
    r"\b(tight\s*ga|kick\s*ga|mass\s*ga|joss\s*ga)\b",
    r"\b(work\s+avutundi|work\s+avvaledu|work\s+aindi)\b",
    r"\b(fix\s+chesanu|fix\s+cheyyi|fix\s+aindi)\b",
    r"\b(error\s+vasthundi|error\s+ochindi)\b",
    r"\b(sort\s+aindi|sort\s+chesanu)\b",
    r"\b(push|deploy|commit)\s+chesanu\b",
    r"\b(test|code)\s+(chesanu|chestunna|pass|fail)\b",
    r"\b(oka|okka)\s+(issue|problem|bug|vishayam)\s+(undi|ledu|aindi)\b",
    r"\b(super\s*ra|superr|joss\s+ga)\b",
    r"\b(ippudu|twaraga|slowly\s+ga)\b",
    r"\b(ela|em|enti)\s+(ra|da|bro)\b",
    r"\b(okay|ok|sarle)\s*(ra|da|bro|machcha)\b",
    r"\bavutundi\b",
    r"\buntundi\b",
    r"\bledu\b",
    r"\b(cheppu|cheppindi|cheppanu)\b",
    r"\b(konchem|koddiga)\b",
    r"\b(problem|issue|tension)\s+ledu\b",
    r"\b(motham|complete)\s+aindi\b",
    r"\bela\s+unnav\b",
    r"\bela\s+undi\b",
    r"\baiyyo\s+bro\b",
]

# Hyderabadi dialect markers (Telugu + Hindi/Urdu)
_HYDERABADI_PATTERNS: list = [
    r"\b(kya\s+re|kya\s+ra|kyaa\s+re)\b",
    r"\b(kya\s+hua|kya\s+hoga|kya\s+baat)\b",
    r"\byaar\b",
    r"\b(seedha|sidha)\b",
    r"\b(nakko|nai|nahi)\b.*\b(karo|karna|baat)\b",
    r"\b(bol|bolo|bolre|bol\s+ra)\b",
    r"\bkya\s+(chahiye|chahte|hua)\b",
    r"\b(mast|badhiya|jhakaas)\b",
    r"\b(lag\s*raha|lag\s*ra|lag\s*rahi)\b",
    r"\b(bilkul|bilkull)\b",
    r"\bkoi\s+baat\s+nahi\b",
    r"\b(yaar|bhai)\b.*\b(ra|da|re)\b",
    r"\b(tension|chinta)\s+(mat|nahi)\b",
    r"\bset\s+hai\b",
    r"\b(chill|relax)\s+maar\b",
    r"\b(ho\s+gaya|ho\s+gayi|ho\s+jayega)\b.*\b(ra|da|re|bro)\b",
    r"\b(fix|sort)\s+ho\s+gaya\b",
    r"\bkya\s+(re|ra)\b",
]


class TeluguDetector:
    """
    High-precision Telugu language variant detector.

    Scoring approach:
      1. Telugu Unicode   -> immediate high-confidence return
      2. Word-ratio score -> Telugu words / total words (robust vs. raw hit count)
      3. Pattern scores   -> Hyderabadi / Tenglish regex hits
      4. Multi-word phrase matching (longer phrases first, greedy)
      5. Guard: require 2+ word hits OR pattern signal
      6. Dialect selection -> highest composite score wins
      7. Minimum confidence threshold: 0.22
    """

    _tenglish_re   = re.compile("|".join(_TENGLISH_PATTERNS),   re.I)
    _hyderabadi_re = re.compile("|".join(_HYDERABADI_PATTERNS), re.I)

    def detect(self, text: str) -> TeluguState:
        if not text or not text.strip():
            return TeluguState()

        text_lower = text.lower()
        cues: list = []

        # -- 1. Telugu Unicode script (strongest signal) ----------------------
        telugu_chars = _TELUGU_UNICODE_RE.findall(text)
        if len(telugu_chars) >= 2:
            conf  = min(1.0, 0.55 + len(telugu_chars) * 0.04)
            ratio = min(1.0, len(telugu_chars) / max(len(text.split()), 1) * 0.5)
            return TeluguState(
                detected=True,
                dialect="telugu_script",
                confidence=round(conf, 2),
                cues=["unicode_telugu"],
                word_ratio=round(ratio, 2),
            )

        # -- 2. Word-ratio analysis --------------------------------------------
        words       = re.findall(r"\b\w+\b", text_lower)
        total_words = max(len(words), 1)

        telugu_word_hits: set = set()

        # Multi-word phrase matching (longer phrases first)
        for phrase in sorted(_ROMANISED_TELUGU_WORDS, key=len, reverse=True):
            if " " in phrase and phrase in text_lower:
                telugu_word_hits.add(phrase)

        # Single-word exact matching
        for word in words:
            if word in _ROMANISED_TELUGU_WORDS:
                telugu_word_hits.add(word)

        telugu_word_count = sum(len(h.split()) for h in telugu_word_hits)
        word_ratio  = min(1.0, telugu_word_count / total_words)
        roman_score = min(1.0, word_ratio * 2.5 + len(telugu_word_hits) * 0.06)

        if telugu_word_hits:
            cues.extend(sorted(telugu_word_hits, key=len, reverse=True)[:4])

        # -- 3. Pattern matching -----------------------------------------------
        hyd_hits      = self._hyderabadi_re.findall(text_lower)
        tenglish_hits = self._tenglish_re.findall(text_lower)

        hyd_score      = min(1.0, len(hyd_hits) * 0.35)
        tenglish_score = min(1.0, len(tenglish_hits) * 0.28 + word_ratio * 0.4)

        if tenglish_hits:
            cues.append("tenglish_pattern")
        if hyd_hits:
            cues.append("hyderabadi_pattern")

        # -- 4. Guard: single word hit alone is NOT enough -------------------
        has_pattern_signal = bool(hyd_hits) or bool(tenglish_hits)
        sufficient_words   = len(telugu_word_hits) >= 2 or word_ratio >= 0.25
        if not has_pattern_signal and not sufficient_words:
            return TeluguState()

        # -- 5. Dialect selection ---------------------------------------------
        if hyd_score >= 0.35 and (roman_score > 0.10 or tenglish_score > 0.15):
            dialect = "hyderabadi"
            score   = min(1.0, hyd_score * 0.6 + tenglish_score * 0.3 + roman_score * 0.2)
        elif tenglish_score >= 0.25:
            dialect = "tenglish"
            score   = min(1.0, tenglish_score * 0.7 + roman_score * 0.4)
        elif roman_score >= 0.25:
            dialect = "romanised"
            score   = roman_score
        else:
            scores  = {
                "hyderabadi": hyd_score,
                "tenglish":   tenglish_score,
                "romanised":  roman_score,
            }
            dialect = max(scores, key=scores.__getitem__)
            score   = scores[dialect]

        # -- 6. Minimum threshold ---------------------------------------------
        if score < 0.22:
            return TeluguState()

        return TeluguState(
            detected=True,
            dialect=dialect,
            confidence=round(min(1.0, score), 2),
            cues=cues[:6],
            word_ratio=round(word_ratio, 2),
        )


# =============================================================================
# Singleton
# =============================================================================

_tel_detector: TeluguDetector | None = None


def get_telugu_detector() -> TeluguDetector:
    global _tel_detector
    if _tel_detector is None:
        _tel_detector = TeluguDetector()
    return _tel_detector


def detect_telugu(text: str) -> TeluguState:
    """Convenience function -- detect Telugu in a message."""
    return get_telugu_detector().detect(text)
