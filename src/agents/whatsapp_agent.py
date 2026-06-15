"""
TRON-X WhatsApp Agent  (Cloud API reader)
───────────────────────────────────────────
Inbound side of the WhatsApp integration.

Because the WhatsApp Cloud API has **no history/poll endpoint**, inbound messages
arrive only as webhook callbacks. This module:

  * parses webhook payloads (messages + delivery statuses + contact names),
  * stores them in a bounded, thread-safe, JSON-persisted ring buffer
    (survives restarts, deduped by message id), and
  * exposes read operations: list / read / conversations / search / summarize.

Outbound sending + read-receipts live in src.system.whatsapp_client.
Config (in .env): WHATSAPP_STORE_PATH, WHATSAPP_STORE_MAX.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.config import settings
from src.core.logger import log


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _iso(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Webhook payload parsing  (pure functions — no I/O, unit-testable)
# ---------------------------------------------------------------------------

def _extract_text_and_media(m: dict) -> dict:
    """
    Given one inbound message object, return a partial dict with the human-
    readable body plus any media metadata. Never raises on unknown shapes.
    """
    mtype = m.get("type", "unknown")
    out: dict[str, Any] = {
        "type": mtype,
        "body": "",
        "media_id": None,
        "mime_type": None,
        "caption": None,
        "filename": None,
        "reply_id": None,
    }

    if mtype == "text":
        out["body"] = (m.get("text") or {}).get("body", "")

    elif mtype in ("image", "video", "audio", "voice", "document", "sticker"):
        media = m.get(mtype) or {}
        out["media_id"] = media.get("id")
        out["mime_type"] = media.get("mime_type")
        out["caption"] = media.get("caption")
        out["filename"] = media.get("filename")
        label = mtype + (f" {out['filename']}" if out["filename"] else "")
        out["body"] = out["caption"] or f"[{label}]"

    elif mtype == "location":
        loc = m.get("location") or {}
        name = loc.get("name") or ""
        addr = loc.get("address") or ""
        coords = f"({loc.get('latitude')},{loc.get('longitude')})"
        out["body"] = f"[location] {name} {addr} {coords}".strip()

    elif mtype == "contacts":
        names = []
        for c in (m.get("contacts") or []):
            nm = (c.get("name") or {}).get("formatted_name")
            if nm:
                names.append(nm)
        out["body"] = "[contacts] " + ", ".join(names) if names else "[contacts]"

    elif mtype == "interactive":
        inter = m.get("interactive") or {}
        itype = inter.get("type")
        reply = inter.get(itype) or {} if itype else {}
        out["body"] = reply.get("title", "") or f"[interactive:{itype}]"
        out["reply_id"] = reply.get("id")

    elif mtype == "button":  # quick-reply button from a template
        btn = m.get("button") or {}
        out["body"] = btn.get("text", "")
        out["reply_id"] = btn.get("payload")

    elif mtype == "reaction":
        rx = m.get("reaction") or {}
        out["body"] = f"[reaction] {rx.get('emoji', '')}".strip()
        # reaction targets another message — surface that as the context id
        out["reply_id"] = rx.get("message_id")

    else:
        out["body"] = f"[{mtype}]"

    return out


def parse_webhook_payload(payload: dict) -> tuple[list[dict], list[dict]]:
    """
    Parse a Cloud API webhook body into (inbound_messages, status_updates).

    Robustness guarantees:
      * iterates ALL entries and ALL changes (not just the first),
      * ignores non-message change fields and non-whatsapp products,
      * isolates per-message failures so one bad item can't drop the batch.
    """
    inbound: list[dict] = []
    statuses: list[dict] = []

    if not isinstance(payload, dict):
        return inbound, statuses

    for entry in payload.get("entry", []) or []:
        for change in (entry.get("changes", []) if isinstance(entry, dict) else []) or []:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            # Only handle the messages webhook field / whatsapp product.
            if value.get("messaging_product") not in (None, "whatsapp"):
                continue
            if not (value.get("messages") or value.get("statuses")):
                continue

            metadata = value.get("metadata") or {}
            our_number = metadata.get("display_phone_number") or metadata.get("phone_number_id") or ""

            # Map wa_id -> profile name from the contacts array.
            name_map: dict[str, str] = {}
            for c in value.get("contacts", []) or []:
                wid = c.get("wa_id")
                if wid:
                    name_map[wid] = (c.get("profile") or {}).get("name", "") or ""

            # ---- inbound messages ----
            for m in value.get("messages", []) or []:
                try:
                    mid = m.get("id")
                    if not mid:
                        continue
                    frm = m.get("from", "")
                    ts = int(m.get("timestamp") or 0)
                    parts = _extract_text_and_media(m)
                    ctx = m.get("context") or {}
                    record = {
                        "id": mid,
                        "direction": "in",
                        "wa_id": frm,                 # the OTHER party (sender)
                        "from": frm,
                        "to": our_number,
                        "name": name_map.get(frm, ""),
                        "type": parts["type"],
                        "body": parts["body"],
                        "media_id": parts["media_id"],
                        "mime_type": parts["mime_type"],
                        "caption": parts["caption"],
                        "filename": parts["filename"],
                        "reply_id": parts["reply_id"],
                        "context_id": ctx.get("id"),
                        "ts": ts,
                        "date": _iso(ts),
                        "status": "received",
                        "read": False,
                        "error": m.get("errors"),
                    }
                    inbound.append(record)
                except Exception as e:  # never let one message kill the batch
                    log.warning(f"[whatsapp] failed to parse inbound message: {e}")

            # ---- delivery statuses (for messages WE sent) ----
            for s in value.get("statuses", []) or []:
                try:
                    sid = s.get("id")
                    if not sid:
                        continue
                    statuses.append({
                        "id": sid,
                        "status": s.get("status", ""),
                        "recipient": s.get("recipient_id"),
                        "ts": int(s.get("timestamp") or 0),
                        "errors": s.get("errors"),
                    })
                except Exception as e:
                    log.warning(f"[whatsapp] failed to parse status: {e}")

    return inbound, statuses


# ---------------------------------------------------------------------------
# Message store — bounded, thread-safe, JSON-persisted
# ---------------------------------------------------------------------------

# Forward progression of outbound delivery state (used to ignore out-of-order
# status webhooks, e.g. a late "delivered" arriving after "read").
_STATUS_ORDER = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}


class MessageStore:
    """A capped ring buffer of WhatsApp messages with O(1) lookup by id."""

    def __init__(self, path: str, max_items: int) -> None:
        self._path = path
        self._max = max(1, int(max_items))
        self._lock = threading.RLock()
        self._dq: deque[dict] = deque()
        self._by_id: dict[str, dict] = {}
        # Dedup window that OUTLIVES buffer eviction: an id stays "seen" even
        # after its message is dropped from the ring buffer, so a re-delivered
        # (duplicate) webhook for an evicted message is not resurrected. Bounded
        # so it can never grow without limit.
        self._seen_cap = max(self._max * 4, self._max + 4096)
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._load()

    def _mark_seen(self, mid: str) -> None:
        if not mid or mid in self._seen_ids:
            return
        self._seen_ids.add(mid)
        self._seen_order.append(mid)
        while len(self._seen_order) > self._seen_cap:
            old = self._seen_order.popleft()
            self._seen_ids.discard(old)

    # ---- persistence ----
    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for msg in data:
                    if isinstance(msg, dict) and msg.get("id"):
                        self._append_locked(msg)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[whatsapp] could not load store ({self._path}): {e}")

    def _persist_locked(self) -> None:
        """Atomic write: temp file in the same dir, then os.replace."""
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(list(self._dq), f, ensure_ascii=False)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError as e:
            log.error(f"[whatsapp] could not persist store: {e}")

    # ---- internal append with eviction + index sync ----
    def _append_locked(self, msg: dict) -> None:
        mid = msg.get("id")
        if not mid or mid in self._by_id:
            return
        self._dq.append(msg)
        self._by_id[mid] = msg
        self._mark_seen(mid)
        while len(self._dq) > self._max:
            old = self._dq.popleft()
            self._by_id.pop(old.get("id"), None)

    # ---- public mutations ----
    def ingest(self, inbound: list[dict], statuses: list[dict]) -> dict:
        added = 0
        updated = 0
        with self._lock:
            for msg in inbound:
                mid = msg.get("id")
                if mid and mid not in self._seen_ids:   # survives buffer eviction
                    self._append_locked(msg)
                    added += 1
            for st in statuses:
                existing = self._by_id.get(st["id"])
                if not existing or existing.get("direction") != "out":
                    continue  # status for a message we didn't originate — ignore
                new_status = st.get("status", "")
                cur = existing.get("status", "")
                if new_status == "failed":
                    if cur != "read":      # a read message can't later "fail"
                        existing["status"] = "failed"
                elif _STATUS_ORDER.get(new_status, -1) >= _STATUS_ORDER.get(cur, -1):
                    existing["status"] = new_status
                if st.get("errors"):
                    existing["error"] = st["errors"]
                updated += 1
            if added or updated:
                self._persist_locked()
        return {"added": added, "status_updated": updated}

    def add_outbound(self, wamid: str, to: str, body: str, msg_type: str = "text") -> None:
        ts = _now_ts()
        record = {
            "id": wamid,
            "direction": "out",
            "wa_id": to,                       # the OTHER party (recipient)
            "from": settings.whatsapp_phone_number_id or "me",
            "to": to,
            "name": "",
            "type": msg_type,
            "body": body,
            "media_id": None,
            "mime_type": None,
            "caption": None,
            "filename": None,
            "reply_id": None,
            "context_id": None,
            "ts": ts,
            "date": _iso(ts),
            "status": "sent",
            "read": True,                      # not applicable to outbound
            "error": None,
        }
        with self._lock:
            self._append_locked(record)
            self._persist_locked()

    def mark_read_local(self, message_id: str) -> bool:
        with self._lock:
            msg = self._by_id.get(message_id)
            if not msg:
                return False
            if msg.get("direction") == "in" and not msg.get("read"):
                msg["read"] = True
                self._persist_locked()
            return True

    # ---- reads (return copies so callers can't mutate internal state) ----
    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self._dq]

    def get(self, message_id: str) -> Optional[dict]:
        with self._lock:
            msg = self._by_id.get(message_id)
            return dict(msg) if msg else None

    def clear(self) -> None:
        with self._lock:
            self._dq.clear()
            self._by_id.clear()
            self._seen_ids.clear()
            self._seen_order.clear()
            self._persist_locked()


_store_singleton: Optional[MessageStore] = None
_store_lock = threading.Lock()


def get_store() -> MessageStore:
    """Process-wide singleton store."""
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = MessageStore(
                    settings.whatsapp_store_path, settings.whatsapp_store_max
                )
    return _store_singleton


# ---------------------------------------------------------------------------
# WhatsAppAgent — async read/summarize façade
# ---------------------------------------------------------------------------

class WhatsAppAgent:
    """Async reader over the message store + LLM summarization."""

    def __init__(self) -> None:
        self._store = get_store()

    def _run(self, fn, *args, **kwargs):
        return asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    # ---- ingest from webhook (offloaded so the event loop isn't blocked) ----
    async def ingest_webhook(self, payload: dict) -> dict:
        def _work():
            inbound, statuses = parse_webhook_payload(payload)
            return self._store.ingest(inbound, statuses)
        try:
            return await self._run(_work)
        except Exception as e:
            log.error(f"[whatsapp] ingest failed: {e}")
            return {"added": 0, "status_updated": 0, "error": str(e)}

    # ---- list messages ----
    async def list_messages(
        self,
        limit: int = 50,
        contact: Optional[str] = None,
        direction: str = "all",        # all | in | out
        unread_only: bool = False,
    ) -> dict:
        from src.system.whatsapp_client import normalize_msisdn

        def _list():
            msgs = self._store.snapshot()
            wa = normalize_msisdn(contact) if contact else None
            out = []
            for m in msgs:
                if wa and normalize_msisdn(m.get("wa_id", "")) != wa:
                    continue
                if direction in ("in", "out") and m.get("direction") != direction:
                    continue
                if unread_only and not (m.get("direction") == "in" and not m.get("read")):
                    continue
                out.append(m)
            out.sort(key=lambda m: m.get("ts", 0), reverse=True)
            return out[:limit]

        messages = await self._run(_list)
        return {"messages": messages, "count": len(messages)}

    async def read_message(self, message_id: str) -> dict:
        msg = await self._run(self._store.get, message_id)
        return msg or {"error": f"Message {message_id} not found"}

    # ---- conversations (grouped by the other party) ----
    async def list_conversations(self, limit: int = 50) -> dict:
        def _convos():
            msgs = self._store.snapshot()
            groups: dict[str, dict] = {}
            for m in msgs:
                wa = m.get("wa_id", "")
                if not wa:
                    continue
                g = groups.setdefault(wa, {
                    "wa_id": wa, "name": "", "last_ts": 0, "last_message": "",
                    "last_direction": "", "unread": 0, "total": 0,
                })
                g["total"] += 1
                if m.get("name"):
                    g["name"] = m["name"]
                if m.get("direction") == "in" and not m.get("read"):
                    g["unread"] += 1
                if m.get("ts", 0) >= g["last_ts"]:
                    g["last_ts"] = m.get("ts", 0)
                    g["last_message"] = (m.get("body", "") or "")[:200]
                    g["last_direction"] = m.get("direction", "")
            convos = sorted(groups.values(), key=lambda c: c["last_ts"], reverse=True)
            for c in convos:
                c["last_date"] = _iso(c["last_ts"])
            return convos[:limit]

        convos = await self._run(_convos)
        return {"conversations": convos, "count": len(convos)}

    async def get_conversation(self, wa_id: str, limit: int = 50) -> dict:
        from src.system.whatsapp_client import normalize_msisdn

        def _conv():
            target = normalize_msisdn(wa_id)
            msgs = [m for m in self._store.snapshot()
                    if normalize_msisdn(m.get("wa_id", "")) == target]
            msgs.sort(key=lambda m: m.get("ts", 0))      # chronological
            return msgs[-limit:]

        messages = await self._run(_conv)
        return {"wa_id": wa_id, "messages": messages, "count": len(messages)}

    async def search(self, query: str, limit: int = 50) -> dict:
        def _search():
            q = (query or "").lower().strip()
            if not q:
                return []
            hits = [
                m for m in self._store.snapshot()
                if q in (m.get("body", "") or "").lower()
                or q in (m.get("name", "") or "").lower()
                or q in (m.get("wa_id", "") or "").lower()
            ]
            hits.sort(key=lambda m: m.get("ts", 0), reverse=True)
            return hits[:limit]

        messages = await self._run(_search)
        return {"query": query, "messages": messages, "count": len(messages)}

    # ---- mark read (local flag + Graph API read-receipt) ----
    async def mark_read(self, message_id: str) -> dict:
        from src.system import whatsapp_client

        found = await self._run(self._store.mark_read_local, message_id)
        if not found:
            return {"success": False, "error": f"Message {message_id} not in store"}
        remote = await whatsapp_client.mark_read(message_id)
        return {
            "success": bool(remote.get("success")),
            "message_id": message_id,
            "local_marked": True,
            "remote": remote,
        }

    # ---- LLM summarization of a conversation ----
    async def summarize_conversation(
        self,
        wa_id: str,
        limit: int = 50,
        persona: str = "jarvis",
    ) -> dict:
        convo = await self.get_conversation(wa_id, limit)
        messages = convo.get("messages", [])
        if not messages:
            return {"summary": "No messages with this contact.", "count": 0, "wa_id": wa_id}

        name = next((m.get("name") for m in reversed(messages) if m.get("name")), "") or wa_id
        lines = []
        for m in messages:
            who = "You" if m.get("direction") == "out" else (m.get("name") or m.get("wa_id"))
            lines.append(f"[{(m.get('date') or '')[:16]}] {who}: {(m.get('body') or '')[:300]}")
        convo_text = "\n".join(lines)

        prompt = (
            f"Summarize this WhatsApp conversation with {name} "
            f"({len(messages)} messages):\n\n{convo_text}\n\n"
            "Provide: a 1-2 sentence summary, key points, and any action items or "
            "questions awaiting your reply."
        )
        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = await orch.chat(
                user_message=prompt,
                session_id="__whatsapp_summary__",
                intent="chat",
                persona=persona,
                max_tokens=800,
            )
            return {
                "summary": result.get("reply", ""),
                "count": len(messages),
                "wa_id": wa_id,
                "contact": name,
            }
        except Exception as e:
            return {"error": f"Summarization failed: {e}", "messages": messages}

    # ---- media download (Cloud API two-step: resolve URL, then fetch bytes) ----
    async def download_media(self, media_id: str, dest_dir: str = "memory/whatsapp_media") -> dict:
        import httpx
        from src.system.whatsapp_client import _headers, _GRAPH_ROOT, _config_error

        cfg = _config_error()
        if cfg:
            return cfg
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                meta = await client.get(
                    f"{_GRAPH_ROOT}/{settings.whatsapp_api_version}/{media_id}",
                    headers=_headers(),
                )
                if meta.status_code // 100 != 2:
                    return {"success": False, "error": f"Media lookup failed: HTTP {meta.status_code}"}
                info = meta.json()
                url = info.get("url")
                if not url:
                    return {"success": False, "error": "No media URL returned"}
                # The media URL also requires the bearer token.
                blob = await client.get(url, headers=_headers())
                if blob.status_code // 100 != 2:
                    return {"success": False, "error": f"Media fetch failed: HTTP {blob.status_code}"}

            os.makedirs(dest_dir, exist_ok=True)
            ext = (info.get("mime_type") or "application/octet-stream").split("/")[-1].split(";")[0]
            path = os.path.join(dest_dir, f"{media_id}.{ext}")
            with open(path, "wb") as f:
                f.write(blob.content)
            return {
                "success": True,
                "media_id": media_id,
                "path": path,
                "mime_type": info.get("mime_type"),
                "size": len(blob.content),
            }
        except httpx.HTTPError as e:
            return {"success": False, "error": f"Network error: {e}"}
