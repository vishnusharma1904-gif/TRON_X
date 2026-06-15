"""
TRON-X Long-Term Memory Commands
----------------------------------
"Memory mode" — lets the user explicitly tell TRON-X to remember (or forget)
a fact PERMANENTLY, across every future conversation/session, not just the
one it was said in.

  REMEMBER  "remember that my dog's name is Rex"
            "remember I have a dentist appointment on Friday at 4pm"
            "please remember my wifi password is Sunshine123"
            "note that my favorite color is blue"
            "keep in mind I'm allergic to peanuts"
            "make a note that the office wifi is TronNet"

  FORGET    "forget that my dog's name is Rex"
            "forget about my dentist appointment"
            "forget my old wifi password"

Facts are stored in ChromaDB's 'knowledge' collection (src/memory/chroma_db.py
:: remember_fact) — a collection that is NOT session-scoped. Every future chat
turn (any session, any intent) runs RAGPipeline.retrieve_knowledge() against
this collection (see src/memory/rag.py and orchestrator.py step 3a), so a
remembered fact surfaces whenever it's relevant — "once told to remember,
remembered forever."

"Recall" queries ("what do you remember about X", "do you remember...") are
NOT handled here — they're ordinary chat messages and are answered by the LLM
using the [Remembered fact] context that retrieve_knowledge() injects.

Design notes (mirrors commands.py):
  * Both parsers are ANCHORED to the start of the message (after stripping a
    polite lead-in), so they only fire on direct "remember ..." commands —
    never on text that merely mentions the word "remember".
  * `parse_remember_command` deliberately does NOT match
    "remember <name> as <number> on whatsapp" — that's handled by
    commands.py::parse_add_contact, which is checked first in
    try_handle_command() and "wins" for that pattern (it requires a phone
    number + WhatsApp context, which a fact statement won't have).
"""
from __future__ import annotations

import re
from typing import Optional

from src.core.logger import log

# ---------------------------------------------------------------------------
# Lead-in stripping (small local copy to avoid a module-level import cycle
# with commands.py)
# ---------------------------------------------------------------------------

_LEADIN = re.compile(
    r"^\s*(?:please|pls|plz|hey|hi|hello|ok|okay|yo|jarvis|friday|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|i\s+want\s+to|i'?d\s+like\s+to|"
    r"i\s+need\s+to|let'?s|kindly|go\s+ahead\s+and)\b[\s,]*",
    re.I,
)


def _strip_leadins(text: str) -> str:
    out = (text or "").strip()
    for _ in range(4):
        new = _LEADIN.sub("", out, count=1).strip()
        if new == out:
            break
        out = new
    return out


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_REMEMBER = re.compile(
    r"^(?:remember|note(?:\s+down)?|keep\s+in\s+mind|"
    r"make\s+a\s+(?:mental\s+)?note)[,:]?\s+(?:that\s+)?(.+)$",
    re.I | re.S,
)

_FORGET = re.compile(
    r"^forget[,:]?\s+(?:that\s+|about\s+)?(.+)$",
    re.I | re.S,
)

_MIN_FACT_LEN = 3


def parse_remember_command(message: str) -> Optional[str]:
    """
    If `message` is an explicit "remember/note/keep in mind ..." command,
    return the extracted fact text. Otherwise return None.
    """
    if not message:
        return None
    t = _strip_leadins(message)
    m = _REMEMBER.match(t)
    if not m:
        return None
    fact = m.group(1).strip().strip(".!").strip()
    return fact if len(fact) >= _MIN_FACT_LEN else None


def parse_forget_command(message: str) -> Optional[str]:
    """
    If `message` is an explicit "forget ..." command, return the extracted
    target description (used as a search query against remembered facts).
    Otherwise return None.
    """
    if not message:
        return None
    t = _strip_leadins(message)
    m = _FORGET.match(t)
    if not m:
        return None
    target = m.group(1).strip().strip(".!?").strip()
    return target if len(target) >= _MIN_FACT_LEN else None


# ---------------------------------------------------------------------------
# Handlers (perform the side effects, return a reply string)
# ---------------------------------------------------------------------------

# A fact that's a near-exact rewrite of an existing one is treated as an
# update rather than a brand-new memory.
_FORGET_DELETE_THRESHOLD = 0.55


async def handle_remember(fact: str, session_id: str = "", persona: str = "jarvis") -> str:
    """Store `fact` permanently in the knowledge collection and acknowledge."""
    from src.memory.chroma_db import get_chroma

    fact = fact.strip().strip(".!").strip()
    if len(fact) < _MIN_FACT_LEN:
        return "What would you like me to remember?"

    chroma = get_chroma()
    await chroma.remember_fact(fact, session_id=session_id, source="user")

    if persona == "friday":
        return f"Got it — I'll remember that: \"{fact}\"."
    return f"Got it, sir. I'll remember that: \"{fact}\"."


async def handle_forget(query: str, persona: str = "jarvis") -> str:
    """Find and permanently delete remembered fact(s) matching `query`."""
    from src.memory.chroma_db import get_chroma

    query = query.strip().strip(".!?").strip()
    if len(query) < _MIN_FACT_LEN:
        return "What should I forget?"

    chroma = get_chroma()
    hits = await chroma.find_facts(query, top_k=3, min_score=_FORGET_DELETE_THRESHOLD)
    if not hits:
        return f"I don't have anything remembered that matches \"{query}\"."

    for h in hits:
        await chroma.forget_fact(h["id"])

    items = "; ".join(f"\"{h['text']}\"" for h in hits)
    log.info(f"[memory] Forgot {len(hits)} fact(s) matching {query!r}")
    if persona == "friday":
        return f"Done — forgotten: {items}"
    return f"Done, sir. Forgotten: {items}"
