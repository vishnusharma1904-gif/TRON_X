"""
TRON-X Natural-Language Action Commands
--------------------------------------------
Lets Jarvis/Friday *act* on spoken commands instead of only replying. Supported:

  BROWSER  "go to bookmyshow and search for movies in kadapa"
           "open wikipedia and find info on black holes"
           "search amazon for wireless earbuds"
           "browse to github.com/openai"
  SEND     "send a whatsapp to 919876543210 saying I'll be late"
           "whatsapp +1 415 555 0100: running late"
           "message mom on whatsapp saying call me"          (name via contacts)
  READ     "what's my latest whatsapp from Hari"
           "summarize my whatsapp chat with Hari"
           "any new whatsapp messages"   /  "show my whatsapp chats"
  CONTACTS "save Hari as 919989019988 on whatsapp"

Design notes (why this is safe):
  * Every command parser is ANCHORED to the start of the message (after an
    optional polite lead-in). This is critical — it stops the parser firing on
    text that merely *mentions* a command, e.g. the episodic-memory prompt
    "Extract structured info... User: send a whatsapp ... Assistant: ...".
  * The orchestrator additionally only calls this for real user turns (not
    internal "__" sessions / non-chat intents).
All parsers are pure functions; try_handle_command performs the side effects.
"""
from __future__ import annotations

import re
from typing import Optional

from src.core.logger import log

# ---------------------------------------------------------------------------
# BROWSER patterns
# ---------------------------------------------------------------------------

# Verbs that mean "navigate to a site"
_BROWSER_NAV_VERBS = re.compile(
    r"^(?:go\s+to|open|browse(?:\s+to)?|navigate(?:\s+to)?|visit|launch|load|access)\s+",
    re.I,
)
# "search <site> for <query>"  /  "search for <query> on <site>"
_BROWSER_SEARCH_ON = re.compile(
    r"^search(?:\s+for)?\s+(.+?)\s+(?:on|in|at|using)\s+(.+)$", re.I
)
# "find <query> on <site>"
_BROWSER_FIND_ON = re.compile(
    r"^(?:find|look\s+up|look\s+for|get)\s+(.+?)\s+(?:on|in|at|from)\s+(.+)$", re.I
)
# Plain "search for X" / "search X" / "google X" with no site — general web search
_PLAIN_SEARCH = re.compile(
    r"^(?:search(?:\s+for)?|google|look\s+up|find\s+info(?:rmation)?\s+(?:on|about)?|"
    r"what(?:'s|\s+is)\s+(?:the\s+)?|who\s+is\s+|tell\s+me\s+about\s+|"
    r"get\s+info(?:rmation)?\s+(?:on|about)?)\s+(.{3,})$",
    re.I
)

# Known site name -> base URL mapping
_SITE_MAP = {
    "bookmyshow":  "https://www.bookmyshow.com",
    "book my show": "https://www.bookmyshow.com",
    "amazon":      "https://www.amazon.in",
    "flipkart":    "https://www.flipkart.com",
    "youtube":     "https://www.youtube.com",
    "wikipedia":   "https://www.wikipedia.org",
    "github":      "https://github.com",
    "google":      "https://www.google.com",
    "reddit":      "https://www.reddit.com",
    "twitter":     "https://www.twitter.com",
    "x.com":       "https://x.com",
    "instagram":   "https://www.instagram.com",
    "linkedin":    "https://www.linkedin.com",
    "news":        "https://news.google.com",
    "imdb":        "https://www.imdb.com",
    "zomato":      "https://www.zomato.com",
    "swiggy":      "https://www.swiggy.com",
    "myntra":      "https://www.myntra.com",
    "naukri":      "https://www.naukri.com",
}

def _resolve_site_url(site_str: str) -> str:
    """Convert a site name or partial URL to a full URL."""
    s = site_str.strip().lower().rstrip("/")
    if s in _SITE_MAP:
        return _SITE_MAP[s]
    # Try longest prefix match
    for key, url in _SITE_MAP.items():
        if s.startswith(key) or key.startswith(s):
            return url
    # Already a URL?
    if re.match(r"https?://", site_str, re.I):
        return site_str.strip()
    # Add https:// if looks like a domain
    if re.match(r"[\w\-]+\.[a-z]{2,}", s):
        return "https://" + s
    return ""


def parse_browser_command(message: str) -> dict | None:
    """
    Detect a browser navigation or search command.
    Returns {action: 'navigate'|'search'|'scrape', url?, query?, site?} or None.
    """
    t = _strip_leadins(message)

    # Pattern: "go to X and [search/find] Y"
    nav_and = re.match(
        r"^(?:go\s+to|open|browse(?:\s+to)?|navigate(?:\s+to)?|visit)\s+"
        r"(.+?)\s+and\s+(?:search(?:\s+for)?|find|look\s+(?:up\s+)?(?:for)?|show)\s+(.+)$",
        t, re.I
    )
    if nav_and:
        site_str, query = nav_and.group(1).strip(), nav_and.group(2).strip()
        url = _resolve_site_url(site_str)
        if url:
            return {"action": "search", "url": url, "site": site_str, "query": query}

    # Pattern: "search <query> on <site>"  /  "search for <query> on <site>"
    m = _BROWSER_SEARCH_ON.match(t)
    if m:
        query, site_str = m.group(1).strip(), m.group(2).strip()
        url = _resolve_site_url(site_str)
        if url:
            return {"action": "search", "url": url, "site": site_str, "query": query}

    # Pattern: "find <query> on <site>"
    m = _BROWSER_FIND_ON.match(t)
    if m:
        query, site_str = m.group(1).strip(), m.group(2).strip()
        url = _resolve_site_url(site_str)
        if url:
            return {"action": "search", "url": url, "site": site_str, "query": query}

    # Pattern: "go to <url/site>" (plain navigation)
    if _BROWSER_NAV_VERBS.match(t):
        rest = _BROWSER_NAV_VERBS.sub("", t).strip()
        url = _resolve_site_url(rest)
        if url:
            return {"action": "navigate", "url": url, "site": rest}

    # Pattern: plain "search for X" / "google X" / "what is X" — general web search
    m = _PLAIN_SEARCH.match(t)
    if m:
        query = m.group(1).strip().rstrip("?")
        if query:
            return {"action": "search", "url": "", "site": "web", "query": query}

    return None


# ---------------------------------------------------------------------------

_WA = re.compile(r"\bwhat'?s?\s*app\b", re.I)

# Optional polite / addressing lead-in we strip before anchoring (e.g.
# "hey jarvis, can you ...", "please ...").
_LEADIN = re.compile(
    r"^\s*(?:please|pls|plz|hey|hi|hello|ok|okay|yo|jarvis|friday|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|i\s+want\s+to|i'?d\s+like\s+to|"
    r"i\s+need\s+to|let'?s|kindly|go\s+ahead\s+and)\b[\s,]*",
    re.I,
)

_SEND_START = re.compile(r"^(?:send|message|msg|text|tell|ping|shoot|whats?\s*app|wa)\b", re.I)


def _strip_leadins(text: str) -> str:
    out = text.strip()
    # peel stacked lead-ins ("hey jarvis can you ...") up to a few times
    for _ in range(4):
        new = _LEADIN.sub("", out, count=1).strip()
        if new == out:
            break
        out = new
    return out


# ---------------------------------------------------------------------------
# SEND
# ---------------------------------------------------------------------------

def parse_whatsapp_command(message: str) -> Optional[dict]:
    """Extract {recipient, body} from a send command, anchored at message start."""
    if not message or not _WA.search(message):
        return None
    head_text = _strip_leadins(message)
    if not _SEND_START.match(head_text):        # must be an imperative send command
        return None

    # split off the body
    body = head = None
    m = re.search(r"\s(?:saying|that\s+says|that|message)\s+(.+)$", head_text, re.I | re.S)
    if m:
        body, head = m.group(1), head_text[:m.start()]
    else:
        m = re.search(r":\s*(.+)$", head_text, re.S)         # "whatsapp <to>: <body>"
        if m:
            body, head = m.group(1), head_text[:m.start()]
        else:
            mq = re.search(r'["“”](.+?)["“”]', head_text, re.S)
            if mq:
                body, head = mq.group(1), head_text[:mq.start()]
    if not body or head is None:
        return None
    body = body.strip().strip('"').strip('“”').strip()
    if not body:
        return None

    rm = re.search(r"\bto\s+(.+)$", head, re.I)
    if rm:
        recipient = rm.group(1)
    else:
        rm2 = re.search(r"^(?:whats?\s*app|wa|message|msg|text|tell|ping|shoot)\s+(.+)$",
                        _strip_leadins(head), re.I)
        if not rm2:
            return None
        recipient = rm2.group(1)

    recipient = re.sub(r"\b(?:on|via|through)\s+what'?s?\s*app\b.*$", "", recipient, flags=re.I)
    recipient = re.sub(r"\bwhat'?s?\s*app\b", "", recipient, flags=re.I)
    recipient = re.sub(r"^(?:a|an|the)\s+", "", recipient.strip(), flags=re.I)
    recipient = recipient.strip().strip(",").strip()

    # Group? e.g. "to the Family group" -> recipient "Family group" -> name "Family"
    is_group = bool(re.search(r"\bgroup\b", recipient, re.I))
    if is_group:
        recipient = re.sub(r"\bgroup\b", "", recipient, flags=re.I)
        recipient = re.sub(r"^(?:a|an|the)\s+", "", recipient.strip(), flags=re.I)
        recipient = recipient.strip().strip(",").strip()

    return {"recipient": recipient, "body": body, "is_group": is_group} if recipient else None


# ---------------------------------------------------------------------------
# CONTACTS
# ---------------------------------------------------------------------------

def parse_add_contact(message: str) -> Optional[dict]:
    """e.g. 'save Hari as 919989019988 on whatsapp', 'add whatsapp contact mom 91...'. """
    if not message or not _WA.search(message):
        return None
    t = _strip_leadins(message)
    m = re.match(r"^(?:save|add|remember|store)\s+(?:whats?app\s+contact\s+)?(.+?)\s+"
                 r"(?:as\s+)?(\+?[\d][\d\s\-()]{5,}\d)\s*(?:on\s+whats?app)?\s*$", t, re.I)
    if not m:
        return None
    name = re.sub(r"\bwhats?app\b", "", m.group(1), flags=re.I).strip().strip(",")
    number = m.group(2)
    if not name:
        return None
    return {"name": name, "number": number}


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------

_READ_START = re.compile(
    r"^(?:what'?s?|whats|read|show|get|give|any|list|summari[sz]e|check|do\s+i\s+have)\b", re.I)


def parse_read_command(message: str) -> Optional[dict]:
    """Return {action, target?} for a read request, else None."""
    if not message or not _WA.search(message):
        return None
    t = _strip_leadins(message)
    if not _READ_START.match(t):
        return None
    low = t.lower()

    # summarize chat with X
    m = re.search(r"summari[sz]e\s+(?:my\s+)?(?:whats?app\s+)?(?:chat|conversation|thread|messages?)\s+"
                  r"with\s+(.+?)[\s.?!]*$", t, re.I)
    if m:
        return {"action": "summarize", "target": _clean_target(m.group(1))}

    # latest / last message from X
    m = re.search(r"(?:latest|last|recent|new)\b.*\bwhats?app\b.*\bfrom\s+(.+?)[\s.?!]*$", t, re.I) \
        or re.search(r"\bwhats?app\b.*\b(?:from|with)\s+(.+?)[\s.?!]*$", t, re.I)
    if m and ("latest" in low or "last" in low or "recent" in low or "from" in low or "read" in low or "show" in low or "get" in low):
        return {"action": "latest", "target": _clean_target(m.group(1))}

    # list my groups ("list my whatsapp groups", "what groups am i in")
    if re.search(r"\bgroups?\b", low) and not re.search(r"\b(from|with|summari[sz]e)\b", low):
        return {"action": "groups_list"}

    # unread
    if re.search(r"\b(unread|new)\b", low) and "whatsapp" in low.replace(" ", ""):
        return {"action": "unread"}

    # list conversations
    if re.search(r"\b(chats|conversations|contacts|threads)\b", low):
        return {"action": "conversations"}

    return None


def _clean_target(s: str) -> str:
    s = re.sub(r"\b(?:on|via|through)\s+what'?s?\s*app\b.*$", "", s, flags=re.I)
    s = re.sub(r"\bwhat'?s?\s*app\b", "", s, flags=re.I)
    return s.strip().strip("?.!,").strip()


# ---------------------------------------------------------------------------
# Recipient / target resolution
# ---------------------------------------------------------------------------

def _resolve_number(recipient: str) -> dict:
    """Recipient string -> {'to': digits} or {'error': msg}. Used for SENDING."""
    import re as _re
    digits = _re.sub(r"\D", "", recipient or "")
    if 7 <= len(digits) <= 15:
        return {"to": digits}

    from src.agents import whatsapp_contacts
    num = whatsapp_contacts.resolve(recipient)
    if num:
        return {"to": num}

    # fall back to people who've messaged you (stored names)
    try:
        from src.agents.whatsapp_agent import get_store
        name = recipient.strip().lower()
        matches: dict[str, str] = {}
        for m in get_store().snapshot():
            nm = (m.get("name") or "").strip().lower()
            if nm and (nm == name or name in nm) and m.get("wa_id"):
                matches[m["wa_id"]] = m.get("name") or m["wa_id"]
        if len(matches) == 1:
            return {"to": next(iter(matches))}
        if len(matches) > 1:
            return {"error": f"More than one '{recipient}' on WhatsApp "
                             f"({', '.join(matches.values())}). Give the number."}
    except Exception as e:
        log.warning(f"[command] contact lookup failed: {e}")

    return {"error": f"I don't have a number for '{recipient}'. Save it with "
                     f"'save {recipient} as <number> on whatsapp', or give the number directly."}


def _resolve_target_waid(target: str) -> dict:
    """Target name/number -> {'wa_id', 'name'} of a stored conversation, or {'error'}."""
    from src.agents.whatsapp_agent import get_store
    snap = get_store().snapshot()
    digits = re.sub(r"\D", "", target or "")
    if 7 <= len(digits) <= 15:
        name = next((m.get("name") for m in snap if m.get("wa_id") == digits and m.get("name")), "")
        return {"wa_id": digits, "name": name or digits}
    key = (target or "").strip().lower()
    # try contacts first (gives a number even if no messages yet)
    from src.agents import whatsapp_contacts
    num = whatsapp_contacts.resolve(target)
    matches: dict[str, str] = {}
    for m in snap:
        nm = (m.get("name") or "").strip().lower()
        if nm and (nm == key or key in nm) and m.get("wa_id"):
            matches[m["wa_id"]] = m.get("name") or m["wa_id"]
    if num and num not in matches:
        matches[num] = target
    if len(matches) == 1:
        wid = next(iter(matches))
        return {"wa_id": wid, "name": matches[wid]}
    if len(matches) > 1:
        return {"error": f"More than one '{target}' ({', '.join(matches.values())})."}
    return {"error": f"I don't see any WhatsApp messages with '{target}'."}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def _handle_browser(cmd: dict, persona: str) -> str:
    """Search the web via Serper API and synthesise a reply via LLM."""
    from src.agents.serper_client import serper_search, format_results_as_text

    action = cmd.get("action")
    url    = cmd.get("url", "")
    query  = cmd.get("query", "")
    site   = cmd.get("site", url)

    # Build the search query
    if action == "search" and query:
        if url:
            # Site-restricted search: "movies in kadapa site:bookmyshow.com"
            domain = url.replace("https://", "").replace("http://", "").rstrip("/")
            search_query = f"{query} site:{domain}"
        else:
            # Plain general web search — no site restriction
            search_query = query
    elif action == "navigate":
        # Just navigating to a site — search for what's on the site's homepage
        domain = url.replace("https://", "").replace("http://", "").rstrip("/")
        search_query = f"{site} official site"
    else:
        search_query = query or site

    try:
        results = await serper_search(search_query, num=6)
    except Exception as e:
        log.error(f"[commands] Serper search failed: {e}")
        return f"I couldn't search the web right now — {e}."

    if not results:
        return f"I searched for '{search_query}' but found no results. Try rephrasing."

    raw_content = format_results_as_text(results)

    # Synthesise a natural-language reply using the orchestrator
    try:
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()
        user_ask = query if query else f"what's on {site}"
        synthesis_prompt = (
            f"The user asked: {user_ask}\n\n"
            f"Here are real web search results from Google (via Serper):\n\n"
            f"{raw_content}\n\n"
            f"Answer the user's question based strictly on the above results. "
            f"Do not add anything not present in the results. Be concise and direct."
        )
        result = await orch.chat(
            user_message=synthesis_prompt,
            session_id=f"__web_search_{id(synthesis_prompt)}__",
            persona=persona,
        )
        # orch.chat() returns a dict {"reply": str, ...} or occasionally a plain str
        if isinstance(result, dict):
            return result.get("reply") or result.get("message") or raw_content
        return str(result)
    except Exception as e:
        log.warning(f"[commands] LLM synthesis failed, returning raw results: {e}")
        return f"Here's what I found about '{user_ask}':\n\n{raw_content}"


async def try_handle_command(message: str, persona: str = "jarvis", session_id: str = "") -> Optional[str]:
    """Perform an action command and return a reply, or None to fall through to chat."""
    # 0) browser navigation / search  (checked FIRST to prevent LLM hallucination)
    browser_cmd = parse_browser_command(message)
    if browser_cmd:
        return await _handle_browser(browser_cmd, persona)

    # 1) add contact (most specific — "save/remember <name> as <number> on whatsapp")
    #    Checked before the generic memory commands below so this WhatsApp-
    #    specific use of "remember" wins (it requires a phone number + WhatsApp
    #    context, which a plain "remember <fact>" statement won't have).
    contact = parse_add_contact(message)
    if contact:
        from src.agents import whatsapp_contacts
        res = whatsapp_contacts.add(contact["name"], contact["number"])
        if "error" in res:
            return f"I couldn't save that contact — {res['error']}."
        return f"Saved {res['name']} → {res['number']} on WhatsApp."

    # 1b) long-term memory — "remember that ..." / "forget that ..."
    #     Stores/deletes facts permanently in the knowledge collection so they
    #     are recalled in ALL future conversations (see memory_commands.py and
    #     RAGPipeline.retrieve_knowledge()).
    from src.intelligence.memory_commands import (
        handle_forget,
        handle_remember,
        parse_forget_command,
        parse_remember_command,
    )
    fact = parse_remember_command(message)
    if fact:
        return await handle_remember(fact, session_id=session_id, persona=persona)
    forget_target = parse_forget_command(message)
    if forget_target:
        return await handle_forget(forget_target, persona=persona)

    # 1c) "clear command cache" / "reset routines" — wipes Phase 22's local
    #     intent-similarity cache (see intent_cache.py). Checked here (not
    #     before 1b) since its trigger words (clear/reset/flush) never
    #     collide with the remember/forget patterns above.
    from src.intelligence.intent_cache import handle_clear_cache, parse_clear_cache_command
    if parse_clear_cache_command(message):
        return await handle_clear_cache(persona=persona)

    # 2) read
    read = parse_read_command(message)
    if read:
        return await _handle_read(read, persona)

    # 3) send
    parsed = parse_whatsapp_command(message)
    if not parsed:
        return None
    if parsed.get("is_group"):
        return await _handle_group_send(parsed["recipient"], parsed["body"], persona)
    resolved = _resolve_number(parsed["recipient"])
    if "error" in resolved:
        return resolved["error"]
    to, body = resolved["to"], parsed["body"]
    from src.system import whatsapp_client
    result = await whatsapp_client.send_text(to, body, confirm=True)
    if result.get("success"):
        ack = "Sent, sir." if persona == "jarvis" else "Done!"
        return f"{ack} WhatsApp to {to}: '{body}'"
    reply = f"I couldn't send that WhatsApp — {result.get('error', 'unknown error')}"
    if result.get("hint"):
        reply += f" ({result['hint']})"
    return reply


def _resolve_group(name: str, groups: list) -> Optional[dict]:
    """Match a group name to {id, subject}: exact subject, else unique substring."""
    key = (name or "").strip().lower()
    if not key:
        return None
    exact = [g for g in groups if (g.get("subject") or "").strip().lower() == key]
    if len(exact) == 1:
        return exact[0]
    subs = [g for g in groups if key in (g.get("subject") or "").lower()]
    if len(subs) == 1:
        return subs[0]
    return None


async def _handle_group_send(name: str, body: str, persona: str) -> str:
    from src.system import whatsapp_client
    res = await whatsapp_client.list_groups()
    if not res.get("success"):
        return f"I couldn't fetch your WhatsApp groups — {res.get('error', 'unknown error')}."
    groups = res.get("groups", [])
    match = _resolve_group(name, groups)
    if not match:
        names = ", ".join(g.get("subject", "") for g in groups[:10] if g.get("subject"))
        msg = f"I couldn't find a WhatsApp group called '{name}'."
        return msg + (f" Your groups: {names}." if names else " You don't seem to be in any groups yet.")
    result = await whatsapp_client.send_group(match["id"], body, confirm=True)
    if result.get("success"):
        ack = "Sent, sir." if persona == "jarvis" else "Done!"
        return f"{ack} WhatsApp to the {match.get('subject') or 'group'} group: '{body}'"
    reply = f"I couldn't send to that group — {result.get('error', 'unknown error')}"
    if result.get("hint"):
        reply += f" ({result['hint']})"
    return reply


async def _handle_read(read: dict, persona: str) -> str:
    from src.agents.whatsapp_agent import WhatsAppAgent, get_store
    action = read["action"]

    if action == "groups_list":
        from src.system import whatsapp_client
        res = await whatsapp_client.list_groups()
        if not res.get("success"):
            return f"I couldn't fetch your WhatsApp groups — {res.get('error', 'unknown error')}."
        groups = res.get("groups", [])
        if not groups:
            return "You're not in any WhatsApp groups (or they haven't loaded yet)."
        names = "; ".join((g.get("subject") or g.get("id", "")) for g in groups[:20])
        return f"Your WhatsApp groups ({len(groups)}): {names}"

    if action == "unread":
        snap = get_store().snapshot()
        groups: dict[str, dict] = {}
        for m in snap:
            if m.get("direction") == "in" and not m.get("read"):
                wid = m.get("wa_id")
                g = groups.setdefault(wid, {"name": m.get("name") or wid, "n": 0})
                g["n"] += 1
                if m.get("name"):
                    g["name"] = m["name"]
        if not groups:
            return "No unread WhatsApp messages."
        total = sum(g["n"] for g in groups.values())
        who = ", ".join(f"{g['name']} ({g['n']})" for g in groups.values())
        return f"You have {total} unread WhatsApp message(s): {who}."

    if action == "conversations":
        res = await WhatsAppAgent().list_conversations(limit=10)
        convos = res.get("conversations", [])
        if not convos:
            return "No WhatsApp conversations yet."
        lines = [f"{c.get('name') or c['wa_id']}"
                 + (f" — {c['unread']} unread" if c.get("unread") else "")
                                 for c in convos]
        return "Recent WhatsApp chats: " + "; ".join(lines)

    target = read.get("target", "")
    resolved = _resolve_target_waid(target)
    if "error" in resolved:
        return resolved["error"]
    wa_id, name = resolved["wa_id"], resolved["name"]

    if action == "summarize":
        res = await WhatsAppAgent().summarize_conversation(wa_id, persona=persona)
        if res.get("error"):
            return f"I couldn't summarize that -- {res['error']}"
        return res.get("summary") or "Nothing to summarize."

    # action == "latest"
    res = await WhatsAppAgent().get_conversation(wa_id, limit=20)
    inbound = [m for m in res.get("messages", []) if m.get("direction") == "in"]
    if not inbound:
        return f"No messages from {name} yet."
    last = inbound[-1]
    when = (last.get("date") or "")[:16].replace("T", " ")
    return f"Latest from {name}{(' at ' + when) if when else ''}: \"{last.get('body', '')}\""


