"""
TRON-X WhatsApp API
─────────────────────
Prefix: /api/whatsapp

Sending backend is selectable (WHATSAPP_BACKEND = baileys | cloud).

Inbound:
    POST /api/whatsapp/bridge/ingest   open-source sidecar -> us (token-auth)
    GET/POST /api/whatsapp/webhook      Meta Cloud API -> us (cloud only)

Reading:  /messages /read /conversations /conversation /search /download
Sending:  /send /send/template /mark/read   (confirm=True required)
Both:     /status /qr /draft /summarize
"""
from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Optional

from src.core.config import settings
from src.core.logger import log

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SendTextReq(BaseModel):
    to: str
    body: str
    preview_url: bool = False
    confirm: bool = False

class SendTemplateReq(BaseModel):
    to: str
    template_name: str
    language: str = "en_US"
    components: Optional[list[dict[str, Any]]] = None
    confirm: bool = False

class MarkReadReq(BaseModel):
    message_id: str

class ListMessagesReq(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    contact: Optional[str] = None
    direction: str = Field(default="all", pattern="^(all|in|out)$")
    unread_only: bool = False

class ReadMessageReq(BaseModel):
    message_id: str

class ConversationReq(BaseModel):
    wa_id: str
    limit: int = Field(default=50, ge=1, le=500)

class SearchReq(BaseModel):
    query: str
    limit: int = Field(default=50, ge=1, le=500)

class SummarizeReq(BaseModel):
    wa_id: str
    limit: int = Field(default=50, ge=1, le=500)
    persona: str = "jarvis"

class DraftReq(BaseModel):
    to: str
    context: str
    tone: str = "friendly"
    persona: str = "jarvis"

class DownloadMediaReq(BaseModel):
    media_id: str


# ---------------------------------------------------------------------------
# Inbound: open-source bridge ingest (POST, token-authenticated)
# ---------------------------------------------------------------------------

def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


@router.post("/bridge/ingest")
async def bridge_ingest(request: Request):
    """
    Receive inbound messages from the Node sidecar. Authenticated with the
    shared WHATSAPP_BRIDGE_TOKEN (constant-time compare). Always 200 on an
    authentic, parseable payload so the sidecar does not retry needlessly.
    """
    from src.agents.whatsapp_agent import get_store
    from src.agents.whatsapp_bridge import parse_bridge_payload

    token = settings.whatsapp_bridge_token
    if not token:
        return JSONResponse({"error": "bridge token not configured"}, status_code=403)
    presented = _bearer(request)
    if not (presented and hmac.compare_digest(presented, token)):
        log.warning("[whatsapp] bridge ingest rejected (bad token)")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    inbound, statuses = parse_bridge_payload(messages)
    result = get_store().ingest(inbound, statuses)
    if result.get("added"):
        log.info("[whatsapp] bridge ingest: +%s msg", result.get("added"))
    return result


# ---------------------------------------------------------------------------
# Inbound: Cloud API webhook (verification GET + receive POST)
# ---------------------------------------------------------------------------

@router.get("/webhook")
async def verify_webhook(request: Request):
    """Cloud API webhook handshake — echo hub.challenge as plain text on match."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = settings.whatsapp_verify_token
    if not expected:
        return PlainTextResponse("verify token not configured", status_code=403)
    if mode == "subscribe" and token is not None and hmac.compare_digest(token, expected):
        log.info("[whatsapp] webhook verified")
        return PlainTextResponse(challenge or "", status_code=200)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Cloud API inbound — validate X-Hub-Signature-256 over the raw body, then ingest."""
    from src.system.whatsapp_client import verify_signature
    from src.agents.whatsapp_agent import WhatsAppAgent

    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    secret = settings.whatsapp_app_secret
    if not verify_signature(raw, signature, secret):
        log.warning("[whatsapp] webhook signature verification FAILED — rejecting")
        return Response(status_code=403)
    if not secret:
        log.warning("[whatsapp] WHATSAPP_APP_SECRET unset — accepting webhook UNVERIFIED (dev only)")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return Response(status_code=200)
    try:
        result = await WhatsAppAgent().ingest_webhook(payload)
        if result.get("added") or result.get("status_updated"):
            log.info("[whatsapp] webhook: +%s msg, %s status updates",
                     result.get("added", 0), result.get("status_updated", 0))
    except Exception as e:
        log.error(f"[whatsapp] webhook processing error: {e}")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Status / QR
# ---------------------------------------------------------------------------

@router.get("/status")
async def whatsapp_status():
    """Backend-aware connection check."""
    from src.system import whatsapp_client
    return await whatsapp_client.connection_status()


@router.get("/qr")
async def whatsapp_qr():
    """Linking QR for the open-source (baileys) backend."""
    from src.system import whatsapp_client
    return await whatsapp_client.get_qr()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@router.post("/messages")
async def list_messages(req: ListMessagesReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().list_messages(
        limit=req.limit, contact=req.contact,
        direction=req.direction, unread_only=req.unread_only,
    )


@router.post("/read")
async def read_message(req: ReadMessageReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().read_message(req.message_id)


@router.get("/conversations")
async def list_conversations(limit: int = 50):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().list_conversations(limit=limit)


@router.post("/conversation")
async def get_conversation(req: ConversationReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().get_conversation(req.wa_id, req.limit)


@router.post("/search")
async def search_messages(req: SearchReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().search(req.query, req.limit)


@router.post("/download")
async def download_media(req: DownloadMediaReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().download_media(req.media_id)


# ---------------------------------------------------------------------------
# Sending  (confirm=True required)
# ---------------------------------------------------------------------------

@router.post("/send")
async def send_text(req: SendTextReq):
    from src.system import whatsapp_client
    return await whatsapp_client.send_text(
        req.to, req.body, preview_url=req.preview_url, confirm=req.confirm,
    )


@router.post("/send/template")
async def send_template(req: SendTemplateReq):
    from src.system import whatsapp_client
    return await whatsapp_client.send_template(
        req.to, req.template_name, language=req.language,
        components=req.components, confirm=req.confirm,
    )


@router.post("/mark/read")
async def mark_read(req: MarkReadReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().mark_read(req.message_id)


@router.post("/draft")
async def draft(req: DraftReq):
    from src.system import whatsapp_client
    return await whatsapp_client.compose_draft(
        req.to, req.context, tone=req.tone, persona=req.persona,
    )


@router.post("/summarize")
async def summarize(req: SummarizeReq):
    from src.agents.whatsapp_agent import WhatsAppAgent
    return await WhatsAppAgent().summarize_conversation(
        req.wa_id, limit=req.limit, persona=req.persona,
    )
