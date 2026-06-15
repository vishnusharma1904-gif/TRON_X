"""
TRON-X Email Agent  (Phase 12)
---------------------------------
IMAP reader with thread parsing and LLM summarization.
Config (in .env):  IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS, IMAP_SSL
Sending is handled by the existing src.system.email_client (SMTP).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import re
import time
from email.header import decode_header as _decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from src.core.config import settings
from src.core.logger import log

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _decode_mime_words(raw: str | bytes | None) -> str:
    """Decode RFC 2047 encoded header words (=?utf-8?...?=)."""
    if raw is None:
        return ""
    parts = _decode_header(raw)
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_body(msg: email.message.Message, prefer_plain: bool = True) -> str:
    """Extract text body from a parsed email.Message."""
    plain_parts: list[str] = []
    html_parts:  list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                plain_parts.append(text)
            elif ct == "text/html":
                html_parts.append(_strip_html(text))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(_strip_html(text))
            else:
                plain_parts.append(text)

    if prefer_plain and plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return "\n".join(html_parts)
    return "\n".join(plain_parts)


def _parse_message(uid: bytes, raw: bytes) -> dict:
    """Parse raw RFC 822 bytes into a structured dict."""
    msg = email.message_from_bytes(raw)
    try:
        date_str = msg.get("Date", "")
        date_iso = parsedate_to_datetime(date_str).isoformat() if date_str else ""
    except Exception:
        date_iso = ""
    return {
        "uid":          uid.decode() if isinstance(uid, bytes) else str(uid),
        "message_id":   msg.get("Message-ID", ""),
        "thread_id":    msg.get("References", msg.get("In-Reply-To", "")),
        "from":         _decode_mime_words(msg.get("From", "")),
        "to":           _decode_mime_words(msg.get("To", "")),
        "cc":           _decode_mime_words(msg.get("Cc", "")),
        "subject":      _decode_mime_words(msg.get("Subject", "(no subject)")),
        "date":         date_iso,
        "body":         _extract_body(msg),
        "has_attachments": any(
            "attachment" in str(p.get("Content-Disposition", ""))
            for p in (msg.walk() if msg.is_multipart() else [msg])
        ),
    }


# ---------------------------------------------------------------------------
# IMAP connection factory
# ---------------------------------------------------------------------------

def _connect() -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    if not (settings.imap_host and settings.imap_user and settings.imap_pass):
        raise RuntimeError(
            "IMAP not configured. Add IMAP_HOST, IMAP_USER, IMAP_PASS to .env"
        )
    if settings.imap_ssl:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
    conn.login(settings.imap_user, settings.imap_pass)
    return conn


# ---------------------------------------------------------------------------
# EmailAgent
# ---------------------------------------------------------------------------

class EmailAgent:
    """Async IMAP email reader + LLM summarizer."""

    def _run(self, fn, *args, **kwargs):
        return asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    # ------------------------------------------------------------------
    # Connection check
    # ------------------------------------------------------------------
    async def connection_status(self) -> dict:
        def _check():
            conn = _connect()
            conn.logout()
            return True
        try:
            await self._run(_check)
            return {
                "connected": True,
                "host": settings.imap_host,
                "user": settings.imap_user,
                "ssl":  settings.imap_ssl,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------
    async def list_folders(self) -> dict:
        def _list():
            conn = _connect()
            _, folders = conn.list()
            conn.logout()
            result = []
            for f in folders:
                parts = _decode(f).split('"')
                name = parts[-1].strip().strip('"') if parts else _decode(f)
                result.append(name)
            return result

        try:
            folders = await self._run(_list)
            return {"folders": folders}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Fetch emails (headers + snippet)
    # ------------------------------------------------------------------
    async def fetch_emails(
        self,
        folder: str = "INBOX",
        limit: int = 20,
        unread_only: bool = False,
        search_query: str = "ALL",
    ) -> dict:
        def _fetch():
            conn = _connect()
            conn.select(f'"{folder}"', readonly=True)

            criteria = "UNSEEN" if unread_only else search_query
            _, uids_raw = conn.uid("SEARCH", None, criteria)
            uids = uids_raw[0].split() if uids_raw[0] else []
            uids = uids[-limit:]  # most recent last

            if not uids:
                conn.logout()
                return []

            uid_list = b",".join(uids)
            _, data = conn.uid("FETCH", uid_list, "(RFC822)")
            conn.logout()

            messages = []
            i = 0
            while i < len(data):
                item = data[i]
                if isinstance(item, tuple) and len(item) == 2:
                    # Extract UID from response line
                    meta = _decode(data[i][0])
                    uid_match = re.search(r"UID\s+(\d+)", meta)
                    uid = uid_match.group(1).encode() if uid_match else b"0"
                    parsed = _parse_message(uid, item[1])
                    # Truncate body to snippet
                    parsed["snippet"] = parsed["body"][:300].replace("\n", " ").strip()
                    del parsed["body"]
                    messages.append(parsed)
                i += 1

            messages.reverse()  # newest first
            return messages

        try:
            messages = await self._run(_fetch)
            return {"folder": folder, "emails": messages, "count": len(messages)}
        except Exception as e:
            log.error(f"[EmailAgent] fetch_emails failed: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Read single email (full body)
    # ------------------------------------------------------------------
    async def read_email(self, uid: str, folder: str = "INBOX") -> dict:
        def _read():
            conn = _connect()
            conn.select(f'"{folder}"', readonly=True)
            _, data = conn.uid("FETCH", uid.encode(), "(RFC822)")
            conn.logout()
            if not data or not isinstance(data[0], tuple):
                return {"error": f"Email UID {uid} not found"}
            return _parse_message(uid.encode(), data[0][1])

        try:
            return await self._run(_read)
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def search_emails(
        self,
        query: str,
        folder: str = "INBOX",
        limit: int = 20,
    ) -> dict:
        """
        IMAP SEARCH wrapper. query examples:
          'FROM "alice@example.com"'
          'SUBJECT "invoice"'
          'SINCE "01-Jun-2026"'
          'TEXT "meeting"'
        """
        def _search():
            conn = _connect()
            conn.select(f'"{folder}"', readonly=True)
            _, uids_raw = conn.uid("SEARCH", None, query)
            uids = uids_raw[0].split() if uids_raw[0] else []
            uids = uids[-limit:]
            if not uids:
                conn.logout()
                return []
            uid_list = b",".join(uids)
            _, data = conn.uid("FETCH", uid_list, "(RFC822)")
            conn.logout()
            messages = []
            i = 0
            while i < len(data):
                item = data[i]
                if isinstance(item, tuple) and len(item) == 2:
                    meta = _decode(data[i][0])
                    uid_match = re.search(r"UID\s+(\d+)", meta)
                    uid_b = uid_match.group(1).encode() if uid_match else b"0"
                    parsed = _parse_message(uid_b, item[1])
                    parsed["snippet"] = parsed["body"][:300].replace("\n", " ").strip()
                    del parsed["body"]
                    messages.append(parsed)
                i += 1
            messages.reverse()
            return messages

        try:
            messages = await self._run(_search)
            return {"query": query, "folder": folder, "emails": messages, "count": len(messages)}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Mark read / unread
    # ------------------------------------------------------------------
    async def mark_read(self, uid: str, folder: str = "INBOX") -> dict:
        def _mark():
            conn = _connect()
            conn.select(f'"{folder}"')
            conn.uid("STORE", uid.encode(), "+FLAGS", r"(\Seen)")
            conn.logout()
        try:
            await self._run(_mark)
            return {"marked_read": True, "uid": uid}
        except Exception as e:
            return {"error": str(e)}

    async def mark_unread(self, uid: str, folder: str = "INBOX") -> dict:
        def _mark():
            conn = _connect()
            conn.select(f'"{folder}"')
            conn.uid("STORE", uid.encode(), "-FLAGS", r"(\Seen)")
            conn.logout()
        try:
            await self._run(_mark)
            return {"marked_unread": True, "uid": uid}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Delete (move to Trash)
    # ------------------------------------------------------------------
    async def delete_email(self, uid: str, folder: str = "INBOX",
                           trash_folder: str = "Trash", confirm: bool = False) -> dict:
        if not confirm:
            return {"error": "Set confirm=True to delete email", "uid": uid}

        def _delete():
            conn = _connect()
            conn.select(f'"{folder}"')
            # Try MOVE (RFC 6851), fall back to COPY+DELETE
            try:
                conn.uid("MOVE", uid.encode(), trash_folder)
            except Exception:
                conn.uid("COPY", uid.encode(), trash_folder)
                conn.uid("STORE", uid.encode(), "+FLAGS", r"(\Deleted)")
                conn.expunge()
            conn.logout()

        try:
            await self._run(_delete)
            return {"deleted": True, "uid": uid, "moved_to": trash_folder}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # LLM summarization
    # ------------------------------------------------------------------
    async def summarize_inbox(
        self,
        folder: str = "INBOX",
        limit: int = 10,
        unread_only: bool = True,
        persona: str = "jarvis",
    ) -> dict:
        """Fetch recent emails and produce an LLM digest."""
        fetch_result = await self.fetch_emails(folder, limit, unread_only)
        if "error" in fetch_result:
            return fetch_result
        emails = fetch_result.get("emails", [])
        if not emails:
            return {"summary": "No new emails.", "count": 0}

        # Build compact email list for LLM
        lines = []
        for i, e in enumerate(emails, 1):
            lines.append(
                f"{i}. From: {e['from'][:60]}  |  Subject: {e['subject'][:80]}"
                f"  |  Date: {e['date'][:10]}"
                f"\n   Snippet: {e.get('snippet','')[:200]}"
            )
        inbox_text = "\n\n".join(lines)

        prompt = (
            f"You have {len(emails)} email(s) in {folder}. Provide a concise digest:\n\n"
            f"{inbox_text}\n\n"
            "For each email: sender, subject, and one-sentence summary. "
            "End with any urgent action items."
        )

        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__email_summary__",
                intent="chat",
                persona=persona,
                max_tokens=800,
            )
            return {
                "summary": result.get("reply", ""),
                "count":   len(emails),
                "folder":  folder,
                "emails":  emails,
            }
        except Exception as e:
            return {"error": f"Summarization failed: {e}", "emails": emails}

    async def summarize_thread(
        self,
        uids: list[str],
        folder: str = "INBOX",
        persona: str = "jarvis",
    ) -> dict:
        """Fetch multiple emails by UID and summarize the thread."""
        messages = []
        for uid in uids:
            result = await self.read_email(uid, folder)
            if "error" not in result:
                messages.append(result)

        if not messages:
            return {"error": "No messages found for given UIDs"}

        messages.sort(key=lambda m: m.get("date", ""))

        thread_text = ""
        for m in messages:
            thread_text += (
                f"--- From: {m['from']}  Date: {m['date']}\n"
                f"Subject: {m['subject']}\n"
                f"{m.get('body','')[:1000]}\n\n"
            )

        prompt = (
            f"Summarize this email thread ({len(messages)} messages):\n\n"
            f"{thread_text}\n\n"
            "Provide: key decisions, action items, timeline of events, and open questions."
        )

        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__thread_summary__",
                intent="chat",
                persona=persona,
                max_tokens=1000,
            )
            return {
                "summary":       result.get("reply", ""),
                "message_count": len(messages),
                "participants":  list({m["from"] for m in messages}),
                "date_range":    f"{messages[0].get('date','')[:10]} — {messages[-1].get('date','')[:10]}",
            }
        except Exception as e:
            return {"error": f"Thread summarization failed: {e}"}

    async def reply_draft(
        self,
        uid: str,
        instructions: str,
        folder: str = "INBOX",
        tone: str = "professional",
        persona: str = "jarvis",
    ) -> dict:
        """Read an email and draft a reply using LLM."""
        original = await self.read_email(uid, folder)
        if "error" in original:
            return original

        prompt = (
            f"Draft a {tone} reply to this email:\n\n"
            f"From: {original['from']}\nSubject: {original['subject']}\n"
            f"Body: {original.get('body','')[:1500]}\n\n"
            f"Instructions: {instructions}\n\n"
            "Return ONLY the reply body text."
        )

        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__email_reply__",
                intent="chat",
                persona=persona,
                max_tokens=600,
            )
            return {
                "draft":       result.get("reply", ""),
                "reply_to":    original["from"],
                "subject":     f"Re: {original['subject']}",
                "original_uid": uid,
                "note":        "Review and call /api/system/email/send to send",
            }
        except Exception as e:
            return {"error": str(e)}
