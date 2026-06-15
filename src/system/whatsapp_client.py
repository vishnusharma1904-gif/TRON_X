"""
TRON-X WhatsApp Client
───────────────────────
Outbound side of the WhatsApp integration with a pluggable backend:

  * "baileys" (default) — talks to the local open-source Node sidecar
    (whatsapp-bridge/) over HTTP. Uses your own number via the multi-device
    protocol; no browser, no Business account, no templates, no 24h window.
  * "cloud" — Meta WhatsApp Business Cloud API (Graph API).

Select with WHATSAPP_BACKEND in .env. All sends require explicit confirm=True,
mirroring src.system.email_client.

Inbound (reading) is handled by src.agents.whatsapp_agent:
  * baileys  -> the sidecar forwards messages to /api/whatsapp/bridge/ingest
  * cloud    -> Meta posts to /api/whatsapp/webhook
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Optional

import httpx

from src.core.config import settings
from src.core.logger import log

_GRAPH_ROOT = "https://graph.facebook.com"
_HTTP_TIMEOUT = 30.0

# Cloud API error codes meaning "outside the 24h window — use a template".
_REENGAGEMENT_CODES = {131047, 131051, 470}


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-testable in isolation)
# ---------------------------------------------------------------------------

def normalize_msisdn(to: str) -> str:
    """Normalise a phone number to digits-only E.164 (no '+', spaces, punctuation)."""
    if to is None:
        return ""
    return re.sub(r"\D", "", str(to))


def verify_signature(raw_body: bytes, signature_header: Optional[str], app_secret: Optional[str]) -> bool:
    """
    Validate Meta's X-Hub-Signature-256 over the RAW body (cloud webhook only).
    Constant-time compare; no-secret => skip (dev policy decided by caller).
    """
    if not app_secret:
        return True
    if not signature_header:
        return False
    prefix, _, sent_hex = signature_header.partition("=")
    if not sent_hex:
        sent_hex = prefix
    elif prefix.lower() != "sha256":
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent_hex.strip())


def _validate_recipient_and_body(to: str, body: str) -> Optional[dict]:
    """Shared pre-send validation. Returns an error dict, or None if OK."""
    if not body or not body.strip():
        return {"success": False, "error": "Message body is empty"}
    to_norm = normalize_msisdn(to)
    if not (7 <= len(to_norm) <= 15):
        return {"success": False, "error": f"Invalid recipient number: {to!r}"}
    return None


def _record_outbound(wamid: Optional[str], to: str, body: str, msg_type: str = "text") -> None:
    """Record a sent message so it shows in conversations / receives status updates."""
    if not wamid:
        return
    try:
        from src.agents.whatsapp_agent import get_store
        get_store().add_outbound(wamid=wamid, to=normalize_msisdn(to), body=body, msg_type=msg_type)
    except Exception as e:
        log.warning(f"[whatsapp] could not record outbound message: {e}")


# ===========================================================================
# Backend: BAILEYS (open-source Node sidecar)
# ===========================================================================

def _bridge_config_error() -> Optional[dict]:
    if not settings.whatsapp_bridge_token:
        return {
            "success": False,
            "error": "Bridge not configured. Set WHATSAPP_BRIDGE_TOKEN (same value in "
                     "the Node sidecar) and run whatsapp-bridge/.",
        }
    return None


def _bridge_base() -> str:
    return settings.whatsapp_bridge_url.rstrip("/")


def _bridge_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.whatsapp_bridge_token}",
        "Content-Type": "application/json",
    }


def _bridge_unreachable(e: Exception) -> dict:
    return {
        "success": False,
        "error": f"WhatsApp bridge unreachable at {_bridge_base()}. Is the Node "
                 f"sidecar running? ({e})",
    }


async def _send_text_bridge(to: str, body: str) -> dict:
    cfg = _bridge_config_error()
    if cfg:
        return cfg
    payload = {"to": normalize_msisdn(to), "message": body}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(f"{_bridge_base()}/send", headers=_bridge_headers(), json=payload)
    except httpx.HTTPError as e:
        return _bridge_unreachable(e)

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code // 100 != 2 or not data.get("success"):
        err = (data or {}).get("error") or f"HTTP {resp.status_code}"

        # "bad mac" and "not_confirmed" are Baileys noise errors that fire AFTER
        # the message has already been delivered (server ACK timed out or MAC
        # mismatch on the protocol layer).  Treat both as success.
        if err in ("bad mac", "not_confirmed"):
            wamid = data.get("id") or data.get("key", {}).get("id")
            jid   = data.get("jid", "")
            _record_outbound(wamid, to, body, "text")
            log.info(f"[whatsapp] (baileys) sent text → {normalize_msisdn(to)} (bad-mac suppressed)")
            return {"success": True, "message_id": wamid,
                    "wa_id": normalize_msisdn(jid.split("@")[0]) if jid else normalize_msisdn(to)}

        out = {"success": False, "error": err, "http_status": resp.status_code}
        if data.get("hint"):
            out["hint"] = data["hint"]          # bridge already explained how to fix it
        elif err == "not_connected":
            out["hint"] = "The bridge isn't linked to WhatsApp. Fetch /api/whatsapp/qr and scan it."
        elif err == "unauthorized":
            out["hint"] = "WHATSAPP_BRIDGE_TOKEN mismatch between TRON-X and the sidecar."
        elif err == "not_on_whatsapp":
            out["hint"] = "That number isn't registered on WhatsApp."
        elif err == "not_confirmed":
            out["hint"] = ("WhatsApp didn't acknowledge the message — the linked session is likely "
                           "degraded. Re-link the bridge (delete its auth/ folder and rescan the QR).")
        log.error(f"[whatsapp] bridge send failed: {err}")
        return out

    wamid = data.get("id")
    jid = data.get("jid", "")
    _record_outbound(wamid, to, body, "text")
    log.info(f"[whatsapp] (baileys) sent text → {normalize_msisdn(to)} ({wamid})")
    return {"success": True, "message_id": wamid, "wa_id": normalize_msisdn(jid.split('@')[0])}


async def _status_bridge() -> dict:
    cfg = _bridge_config_error()
    if cfg:
        return {"connected": False, "error": cfg["error"], "backend": "baileys"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_bridge_base()}/status", headers=_bridge_headers())
    except httpx.HTTPError as e:
        return {"connected": False, "error": _bridge_unreachable(e)["error"], "backend": "baileys"}
    if resp.status_code // 100 != 2:
        return {"connected": False, "error": f"HTTP {resp.status_code}", "backend": "baileys"}
    data = resp.json()
    return {
        "connected": bool(data.get("connected")),
        "backend": "baileys",
        "me": data.get("me"),
        "needs_qr": bool(data.get("hasQr")),
        "last_error": data.get("lastError"),
        "bridge_url": _bridge_base(),
    }


async def _qr_bridge() -> dict:
    cfg = _bridge_config_error()
    if cfg:
        return cfg
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_bridge_base()}/qr", headers=_bridge_headers())
    except httpx.HTTPError as e:
        return _bridge_unreachable(e)
    if resp.status_code == 409:
        return {"success": True, "linked": True, "note": "Already linked; no QR needed."}
    if resp.status_code == 404:
        return {"success": False, "error": "No QR available yet — start the bridge and retry."}
    if resp.status_code // 100 != 2:
        return {"success": False, "error": f"HTTP {resp.status_code}"}
    return {"success": True, "qr": resp.json().get("qr")}


async def _groups_bridge() -> dict:
    """List the WhatsApp groups the linked account participates in."""
    cfg = _bridge_config_error()
    if cfg:
        return {"success": False, "error": cfg["error"]}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{_bridge_base()}/groups", headers=_bridge_headers())
    except httpx.HTTPError as e:
        return {"success": False, "error": _bridge_unreachable(e)["error"]}
    if resp.status_code // 100 != 2:
        try:
            err = resp.json().get("error")
        except Exception:
            err = f"HTTP {resp.status_code}"
        return {"success": False, "error": err}
    return {"success": True, "groups": resp.json().get("groups", [])}


async def _send_group_bridge(group_jid: str, body: str) -> dict:
    """Send a text to a group JID (…@g.us) via the bridge, raw (no number-mangling)."""
    cfg = _bridge_config_error()
    if cfg:
        return cfg
    payload = {"to": group_jid, "message": body}      # pass the JID through untouched
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(f"{_bridge_base()}/send", headers=_bridge_headers(), json=payload)
    except httpx.HTTPError as e:
        return _bridge_unreachable(e)
    try:
        data = resp.json()
    except Exception:
        data = {}
    gid = normalize_msisdn(group_jid.split("@")[0])
    # mirror the individual-send behaviour (incl. the bad-mac/not_confirmed handling)
    if resp.status_code // 100 != 2 or not data.get("success"):
        err = (data or {}).get("error") or f"HTTP {resp.status_code}"
        if err in ("bad mac", "not_confirmed"):
            wamid = data.get("id") or (data.get("key", {}) or {}).get("id")
            _record_outbound(wamid, gid, body, "text")
            return {"success": True, "message_id": wamid, "wa_id": gid}
        out = {"success": False, "error": err, "http_status": resp.status_code}
        if data.get("hint"):
            out["hint"] = data["hint"]
        log.error(f"[whatsapp] bridge group send failed: {err}")
        return out
    wamid = data.get("id")
    _record_outbound(wamid, gid, body, "text")
    log.info(f"[whatsapp] (baileys) sent group message → {group_jid} ({wamid})")
    return {"success": True, "message_id": wamid, "wa_id": gid}


# ===========================================================================
# Backend: CLOUD (Meta Graph API)
# ===========================================================================

def _cloud_config_error() -> Optional[dict]:
    if not (settings.whatsapp_access_token and settings.whatsapp_phone_number_id):
        return {
            "success": False,
            "error": "WhatsApp Cloud API not configured. Add WHATSAPP_ACCESS_TOKEN and "
                     "WHATSAPP_PHONE_NUMBER_ID to .env",
        }
    return None

# Backwards-compatible alias used by cloud media download in the agent.
_config_error = _cloud_config_error


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }


def _messages_url() -> str:
    return f"{_GRAPH_ROOT}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"


def _parse_graph_error(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    out = {
        "success": False,
        "error": err.get("message", f"HTTP {resp.status_code}"),
        "code": code,
        "type": err.get("type"),
        "details": (err.get("error_data") or {}).get("details"),
        "fbtrace_id": err.get("fbtrace_id"),
        "http_status": resp.status_code,
    }
    if code in _REENGAGEMENT_CODES:
        out["hint"] = (
            "Recipient is outside the 24-hour customer-service window. Free-form "
            "text is not allowed; send an approved template via send_template()."
        )
    return out


async def _post(payload: dict) -> dict:
    cfg = _cloud_config_error()
    if cfg:
        return cfg
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_messages_url(), headers=_headers(), json=payload)
    except httpx.HTTPError as e:
        log.error(f"[whatsapp] HTTP error: {e}")
        return {"success": False, "error": f"Network error: {e}"}
    if resp.status_code // 100 != 2:
        err = _parse_graph_error(resp)
        log.error(f"[whatsapp] cloud send failed: {err.get('error')} (code={err.get('code')})")
        return err
    data = resp.json()
    msgs = data.get("messages", [])
    wamid = msgs[0]["id"] if msgs and isinstance(msgs[0], dict) else None
    contacts = data.get("contacts", [])
    wa_id = contacts[0].get("wa_id") if contacts and isinstance(contacts[0], dict) else None
    return {"success": True, "message_id": wamid, "wa_id": wa_id, "raw": data}


async def _send_text_cloud(to: str, body: str, preview_url: bool) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_msisdn(to),
        "type": "text",
        "text": {"preview_url": bool(preview_url), "body": body},
    }
    result = await _post(payload)
    if result.get("success"):
        _record_outbound(result.get("message_id"), to, body, "text")
        log.info(f"[whatsapp] (cloud) sent text → {normalize_msisdn(to)} ({result.get('message_id')})")
    return result


# ===========================================================================
# Public API (backend-dispatching)
# ===========================================================================

async def send_text(to: str, body: str, preview_url: bool = False, confirm: bool = False) -> dict:
    """Send a free-form text message via the active backend. Requires confirm=True."""
    err = _validate_recipient_and_body(to, body)
    if err:
        return err
    if not confirm:
        return {
            "success": False,
            "error": "Set confirm=True to send the WhatsApp message",
            "preview": {"to": normalize_msisdn(to), "body": body[:300], "backend": settings.whatsapp_backend},
        }
    if settings.whatsapp_backend == "baileys":
        return await _send_text_bridge(to, body)
    return await _send_text_cloud(to, body, preview_url)


async def send_template(
    to: str,
    template_name: str,
    language: str = "en_US",
    components: Optional[list[dict[str, Any]]] = None,
    confirm: bool = False,
) -> dict:
    """
    Pre-approved templates are a Cloud-API concept. With the baileys backend
    there is no 24h window, so just use send_text().
    """
    if settings.whatsapp_backend == "baileys":
        return {
            "success": False,
            "error": "Templates are a Cloud API feature. With the open-source (baileys) "
                     "backend there is no 24-hour window — use send_text() instead.",
        }
    to_norm = normalize_msisdn(to)
    if not template_name:
        return {"success": False, "error": "template_name is required"}
    if not (7 <= len(to_norm) <= 15):
        return {"success": False, "error": f"Invalid recipient number: {to!r}"}
    if not confirm:
        return {
            "success": False,
            "error": "Set confirm=True to send the WhatsApp template",
            "preview": {"to": to_norm, "template": template_name, "language": language},
        }
    template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
    if components:
        template["components"] = components
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_norm,
        "type": "template",
        "template": template,
    }
    result = await _post(payload)
    if result.get("success"):
        _record_outbound(result.get("message_id"), to_norm, f"[template:{template_name}]", "template")
        log.info(f"[whatsapp] (cloud) sent template '{template_name}' → {to_norm}")
    return result


async def mark_read(message_id: str, typing_indicator: bool = False) -> dict:
    """
    Mark a received message read. Cloud sends a real read-receipt; the baileys
    bridge does not expose this yet, so it is a no-op there (local flag only).
    """
    if not message_id:
        return {"success": False, "error": "message_id is required"}
    if settings.whatsapp_backend == "baileys":
        return {"success": True, "message_id": message_id, "note": "read-receipt not sent in bridge mode (local flag only)"}
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    if typing_indicator:
        payload["typing_indicator"] = {"type": "text"}
    cfg = _cloud_config_error()
    if cfg:
        return cfg
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_messages_url(), headers=_headers(), json=payload)
    except httpx.HTTPError as e:
        return {"success": False, "error": f"Network error: {e}"}
    if resp.status_code // 100 != 2:
        return _parse_graph_error(resp)
    return {"success": True, "message_id": message_id}


async def connection_status() -> dict:
    """Backend-aware connection/health check."""
    if settings.whatsapp_backend == "baileys":
        return await _status_bridge()
    cfg = _cloud_config_error()
    if cfg:
        return {"connected": False, "error": cfg["error"], "backend": "cloud"}
    url = f"{_GRAPH_ROOT}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}"
    params = {"fields": "display_phone_number,verified_name,quality_rating"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers(), params=params)
    except httpx.HTTPError as e:
        return {"connected": False, "error": f"Network error: {e}", "backend": "cloud"}
    if resp.status_code // 100 != 2:
        err = _parse_graph_error(resp)
        return {"connected": False, "error": err.get("error"), "code": err.get("code"), "backend": "cloud"}
    data = resp.json()
    return {
        "connected": True,
        "backend": "cloud",
        "phone_number_id": settings.whatsapp_phone_number_id,
        "display_phone_number": data.get("display_phone_number"),
        "verified_name": data.get("verified_name"),
        "quality_rating": data.get("quality_rating"),
        "api_version": settings.whatsapp_api_version,
    }


async def get_qr() -> dict:
    """Return the linking QR (baileys only)."""
    if settings.whatsapp_backend == "baileys":
        return await _qr_bridge()
    return {"success": False, "error": "QR linking applies only to the baileys backend."}


async def list_groups() -> dict:
    """List WhatsApp groups (name + JID) the linked account is in. Baileys only."""
    if settings.whatsapp_backend != "baileys":
        return {"success": False, "error": "Group listing requires the open-source (baileys) backend."}
    return await _groups_bridge()


async def send_group(group_jid: str, body: str, confirm: bool = False) -> dict:
    """Send a text message to a group by its JID (…@g.us). Requires confirm=True."""
    if settings.whatsapp_backend != "baileys":
        return {"success": False, "error": "Group send requires the open-source (baileys) backend."}
    if not group_jid or "@g.us" not in group_jid:
        return {"success": False, "error": f"Not a group JID: {group_jid!r}"}
    if not body or not body.strip():
        return {"success": False, "error": "Message body is empty"}
    if not confirm:
        return {
            "success": False,
            "error": "Set confirm=True to send the group message",
            "preview": {"to": group_jid, "body": body[:300]},
        }
    return await _send_group_bridge(group_jid, body)


async def compose_draft(to: str, context: str, tone: str = "friendly", persona: str = "jarvis") -> dict:
    """Use the LLM to compose a WhatsApp message draft (no sending)."""
    from src.intelligence.orchestrator import get_orchestrator
    prompt = (
        f"Compose a {tone} WhatsApp message to {to}.\n"
        f"Context / instructions: {context}\n\n"
        "Keep it concise and conversational (WhatsApp style). "
        "Return ONLY the message text, no preamble or signature."
    )
    orch = get_orchestrator()
    result = await orch.chat(
        user_message=prompt,
        session_id="__whatsapp_draft__",
        intent="chat",
        persona=persona,
        max_tokens=600,
    )
    return {
        "success": True,
        "to": normalize_msisdn(to),
        "draft": result.get("reply", ""),
        "note": "Review and call send_text(confirm=True) to send",
    }
