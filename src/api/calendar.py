"""
TRON-X Calendar + Reminder API  (Phase 11)
-------------------------------------------
Prefix: /api/calendar
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional

from src.core.logger import log

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateEventReq(BaseModel):
    title: str
    start: str                          # ISO 8601, e.g. "2026-06-10T14:00:00+00:00"
    end: str
    description: str = ""
    location: str = ""
    attendees: Optional[list[str]] = None
    calendar_id: str = "primary"
    all_day: bool = False

class UpdateEventReq(BaseModel):
    event_id: str
    calendar_id: str = "primary"
    title: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None

class DeleteEventReq(BaseModel):
    event_id: str
    calendar_id: str = "primary"

class FreeSlotsReq(BaseModel):
    date: str                           # YYYY-MM-DD
    duration_minutes: int = 30
    work_start: int = Field(default=9, ge=0, le=23)
    work_end: int = Field(default=18, ge=1, le=24)
    calendar_id: str = "primary"

class ReminderReq(BaseModel):
    message: str
    fire_at: Optional[str] = None       # ISO datetime
    delay_seconds: Optional[int] = None
    title: str = "TRON-X Reminder"
    reminder_id: Optional[str] = None

class ReminderNLReq(BaseModel):
    message: str
    when: str                           # e.g. "in 30 minutes", "tomorrow at 9am"
    title: str = "TRON-X Reminder"

class CancelReminderReq(BaseModel):
    reminder_id: str


# ---------------------------------------------------------------------------
# Calendar routes
# ---------------------------------------------------------------------------

@router.get("/auth/status")
async def calendar_auth_status():
    """Check if Google Calendar is authenticated."""
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().auth_status()


@router.post("/auth/connect")
async def calendar_auth_connect():
    """
    Trigger OAuth2 flow. Opens a browser window for Google sign-in.
    Run this once; token is cached for future requests.
    """
    from src.agents.calendar_agent import CalendarAgent
    agent = CalendarAgent()
    try:
        ok = await agent.is_authenticated()
        if ok:
            return {"status": "already_authenticated"}
        # Force re-auth by running the flow
        def _flow():
            from src.agents.calendar_agent import _get_service
            _get_service()
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, _flow)
        return {"status": "authenticated"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/events")
async def list_events(
    days: int = 7,
    max_results: int = 20,
    calendar_id: str = "primary",
):
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().list_events(days, max_results, calendar_id)


@router.post("/events")
async def create_event(req: CreateEventReq):
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().create_event(
        title=req.title,
        start=req.start,
        end=req.end,
        description=req.description,
        location=req.location,
        attendees=req.attendees,
        calendar_id=req.calendar_id,
        all_day=req.all_day,
    )


@router.patch("/events")
async def update_event(req: UpdateEventReq):
    from src.agents.calendar_agent import CalendarAgent
    kwargs = {k: v for k, v in req.model_dump().items()
              if k not in ("event_id", "calendar_id") and v is not None}
    return await CalendarAgent().update_event(req.event_id, req.calendar_id, **kwargs)


@router.delete("/events/{event_id}")
async def delete_event(event_id: str, calendar_id: str = "primary"):
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().delete_event(event_id, calendar_id)


@router.post("/free-slots")
async def find_free_slots(req: FreeSlotsReq):
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().find_free_slots(
        date=req.date,
        duration_minutes=req.duration_minutes,
        work_start=req.work_start,
        work_end=req.work_end,
        calendar_id=req.calendar_id,
    )


@router.get("/calendars")
async def list_calendars():
    from src.agents.calendar_agent import CalendarAgent
    return await CalendarAgent().list_calendars()


# ---------------------------------------------------------------------------
# Reminder routes
# ---------------------------------------------------------------------------

@router.post("/reminders")
async def set_reminder(req: ReminderReq):
    from src.agents.reminder_agent import get_reminder_agent
    return await get_reminder_agent().set_reminder(
        message=req.message,
        fire_at=req.fire_at,
        delay_seconds=req.delay_seconds,
        title=req.title,
        reminder_id=req.reminder_id,
    )


@router.post("/reminders/nl")
async def set_reminder_nl(req: ReminderNLReq):
    """Set a reminder with a natural-language time expression."""
    from src.agents.reminder_agent import get_reminder_agent
    return await get_reminder_agent().set_reminder_nl(
        message=req.message,
        when_nl=req.when,
        title=req.title,
    )


@router.get("/reminders")
async def list_reminders(include_fired: bool = False):
    from src.agents.reminder_agent import get_reminder_agent
    return await get_reminder_agent().list_reminders(include_fired)


@router.delete("/reminders/{reminder_id}")
async def cancel_reminder(reminder_id: str):
    from src.agents.reminder_agent import get_reminder_agent
    return await get_reminder_agent().cancel_reminder(reminder_id)


@router.post("/reminders/{reminder_id}/fire")
async def fire_reminder_now(reminder_id: str):
    """Immediately trigger a reminder (for testing)."""
    from src.agents.reminder_agent import get_reminder_agent
    return await get_reminder_agent().fire_now(reminder_id)
