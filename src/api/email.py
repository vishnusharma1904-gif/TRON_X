"""
TRON-X Email API  (Phase 12)
------------------------------
Prefix: /api/email
IMAP read + LLM summarization. Sending uses /api/system/email/send (SMTP).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional

router = APIRouter(prefix="/api/email", tags=["email"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class FetchEmailsReq(BaseModel):
    folder: str = "INBOX"
    limit: int = Field(default=20, ge=1, le=100)
    unread_only: bool = False

class ReadEmailReq(BaseModel):
    uid: str
    folder: str = "INBOX"

class SearchEmailsReq(BaseModel):
    query: str                      # raw IMAP SEARCH criteria
    folder: str = "INBOX"
    limit: int = Field(default=20, ge=1, le=100)

class MarkReq(BaseModel):
    uid: str
    folder: str = "INBOX"

class DeleteEmailReq(BaseModel):
    uid: str
    folder: str = "INBOX"
    trash_folder: str = "Trash"
    confirm: bool = False

class SummarizeInboxReq(BaseModel):
    folder: str = "INBOX"
    limit: int = Field(default=10, ge=1, le=50)
    unread_only: bool = True
    persona: str = "jarvis"

class SummarizeThreadReq(BaseModel):
    uids: list[str]
    folder: str = "INBOX"
    persona: str = "jarvis"

class ReplyDraftReq(BaseModel):
    uid: str
    instructions: str
    folder: str = "INBOX"
    tone: str = "professional"
    persona: str = "jarvis"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def email_status():
    """Check IMAP connection."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().connection_status()


@router.get("/folders")
async def list_folders():
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().list_folders()


@router.post("/fetch")
async def fetch_emails(req: FetchEmailsReq):
    """Fetch email headers + snippets from a folder."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().fetch_emails(req.folder, req.limit, req.unread_only)


@router.post("/read")
async def read_email(req: ReadEmailReq):
    """Fetch a single email with full body by UID."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().read_email(req.uid, req.folder)


@router.post("/search")
async def search_emails(req: SearchEmailsReq):
    """
    IMAP SEARCH. query examples:
      'FROM "alice@example.com"'
      'SUBJECT "invoice"'
      'SINCE "01-Jun-2026"'
      'TEXT "project update"'
      'UNSEEN'
    """
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().search_emails(req.query, req.folder, req.limit)


@router.post("/mark/read")
async def mark_read(req: MarkReq):
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().mark_read(req.uid, req.folder)


@router.post("/mark/unread")
async def mark_unread(req: MarkReq):
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().mark_unread(req.uid, req.folder)


@router.delete("/delete")
async def delete_email(req: DeleteEmailReq):
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().delete_email(
        req.uid, req.folder, req.trash_folder, req.confirm
    )


@router.post("/summarize/inbox")
async def summarize_inbox(req: SummarizeInboxReq):
    """Fetch recent emails and return an LLM-generated digest."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().summarize_inbox(
        req.folder, req.limit, req.unread_only, req.persona
    )


@router.post("/summarize/thread")
async def summarize_thread(req: SummarizeThreadReq):
    """Fetch multiple emails by UID and summarize the thread."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().summarize_thread(req.uids, req.folder, req.persona)


@router.post("/reply/draft")
async def reply_draft(req: ReplyDraftReq):
    """Read an email and generate a reply draft via LLM."""
    from src.agents.email_agent import EmailAgent
    return await EmailAgent().reply_draft(
        req.uid, req.instructions, req.folder, req.tone, req.persona
    )
