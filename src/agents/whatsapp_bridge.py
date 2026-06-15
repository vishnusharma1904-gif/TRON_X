"""
TRON-X WhatsApp Bridge ingest parser
─────────────────────────────────────
Converts the compact, normalized message records POSTed by the open-source
Node sidecar (whatsapp-bridge/) into the internal store record schema used by
src.agents.whatsapp_agent. Pure function — no I/O — so it is unit-testable.

Sidecar record shape (per message):
    {id, from_me, jid, participant, push_name, ts, type, media, body}
"""
from __future__ import annotations

import re
from typing import Any

from src.core.config import settings
from src.core.logger import log
from src.agents.whatsapp_agent import _iso


def _digits(jid: str) -> str:
    """Extract the phone digits from a JID like '14155550100@s.whatsapp.net'."""
    if not jid:
        return ""
    local = jid.split("@", 1)[0]
    # group JIDs look like '12036-1622@g.us'; keep digits only
    return re.sub(r"\D", "", local)


def parse_bridge_payload(messages: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Return (inbound_records, status_updates) for the store to ingest."""
    inbound: list[dict] = []
    statuses: list[dict] = []
    me = settings.whatsapp_phone_number_id or "me"

    for m in messages or []:
        try:
            mid = m.get("id")
            if not mid:
                continue
            from_me = bool(m.get("from_me"))
            jid = m.get("jid", "") or ""
            num = _digits(jid)
            ts = int(m.get("ts") or 0)
            record = {
                "id": mid,
                "direction": "out" if from_me else "in",
                "wa_id": num,                              # the OTHER party
                "from": me if from_me else num,
                "to": num if from_me else me,
                "name": m.get("push_name", "") or "",
                "type": m.get("type", "unknown"),
                "body": m.get("body", "") or "",
                "media_id": None,                          # bridge media fetched separately
                "mime_type": None,
                "caption": None,
                "filename": None,
                "reply_id": None,
                "context_id": None,
                "ts": ts,
                "date": _iso(ts),
                "status": "sent" if from_me else "received",
                "read": True if from_me else False,
                "error": None,
                "is_group": jid.endswith("@g.us"),
            }
            inbound.append(record)
        except Exception as e:
            log.warning(f"[whatsapp] failed to parse bridge message: {e}")

    return inbound, statuses
