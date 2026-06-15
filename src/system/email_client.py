"""
TRON-X Email Client
────────────────────
Compose and send email via SMTP. Config read from settings/.env.
All sends require explicit 'confirm=True'.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from typing import Optional

from src.core.logger import log
from src.core.config import settings


def _build_mime(
    to: str | list[str],
    subject: str,
    body: str,
    html: bool = False,
    cc: Optional[list[str]] = None,
    reply_to: Optional[str] = None,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"]    = settings.smtp_from or settings.smtp_user
    msg["To"]      = ", ".join(to) if isinstance(to, list) else to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to

    if html:
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


async def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    html: bool = False,
    cc: Optional[list[str]] = None,
    confirm: bool = False,
) -> dict:
    """
    Send an email via SMTP.
    Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in .env.
    Requires confirm=True.
    """
    if not confirm:
        return {
            "success": False,
            "error": "Set confirm=True to send email",
            "preview": {"to": to, "subject": subject, "body": body[:200]},
        }

    # Check config
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        return {
            "success": False,
            "error": "SMTP not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASS to .env",
        }

    msg  = _build_mime(to, subject, body, html=html, cc=cc)
    host = settings.smtp_host
    port = settings.smtp_port  # default 587

    recipients = ([to] if isinstance(to, str) else to) + (cc or [])

    def _smtp_send():
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(msg["From"], recipients, msg.as_string())

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _smtp_send)
        log.info(f"[email] Sent '{subject}' → {to}")
        return {"success": True, "to": to, "subject": subject}
    except Exception as e:
        log.error(f"[email] Send failed: {e}")
        return {"success": False, "error": str(e)}


async def compose_draft(
    to: str,
    subject: str,
    context: str,
    tone: str = "professional",
    persona: str = "jarvis",
) -> dict:
    """
    Use LLM to compose an email draft (no sending).
    Returns the draft text for review.
    """
    from src.intelligence.orchestrator import get_orchestrator

    prompt = (
        f"Compose a {tone} email to {to}.\n"
        f"Subject: {subject}\n"
        f"Context / instructions: {context}\n\n"
        "Return ONLY the email body text, no greeting boilerplate or subject line."
    )
    orch = get_orchestrator()
    result = await orch.chat(
        user_message=prompt,
        session_id="__email_draft__",
        intent="chat",
        persona=persona,
        max_tokens=800,
    )
    draft = result.get("reply", "")
    return {
        "success": True,
        "to": to,
        "subject": subject,
        "draft": draft,
        "note": "Review and call send_email(confirm=True) to send",
    }
