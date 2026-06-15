"""
TRON-X Calendar Agent  (Phase 11)
-----------------------------------
Google Calendar OAuth2 integration.
Token is stored locally at  ~/.tronx/gcal_token.json.
Credentials JSON (from Google Cloud Console) must be at ~/.tronx/gcal_credentials.json
or set GOOGLE_CREDENTIALS_PATH env var.

Auto-installs required packages on first use.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.core.logger import log

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_TOKEN_PATH = Path.home() / ".tronx" / "gcal_token.json"
_CREDS_PATH = Path(os.getenv("GOOGLE_CREDENTIALS_PATH",
                             str(Path.home() / ".tronx" / "gcal_credentials.json")))


def _ensure_packages():
    try:
        import google.auth  # noqa: F401
        import googleapiclient  # noqa: F401
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "google-auth", "google-auth-oauthlib",
             "google-auth-httplib2", "google-api-python-client",
             "--break-system-packages", "--quiet"],
            check=True,
        )


def _get_service():
    """Return an authenticated Google Calendar service object (sync)."""
    _ensure_packages()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _CREDS_PATH.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {_CREDS_PATH}. "
                    "Download OAuth 2.0 credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_PATH), _SCOPES)
            creds = flow.run_local_server(port=0)

        _TOKEN_PATH.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


class CalendarAgent:
    """Async wrapper around Google Calendar API."""

    def _loop_run(self, fn, *args, **kwargs):
        return asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    async def is_authenticated(self) -> bool:
        try:
            def _check():
                _get_service()
                return True
            return await self._loop_run(_check)
        except Exception:
            return False

    async def auth_status(self) -> dict:
        token_exists = _TOKEN_PATH.exists()
        creds_exists = _CREDS_PATH.exists()
        authenticated = False
        if token_exists:
            try:
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)
                authenticated = creds.valid or bool(creds.refresh_token)
            except Exception:
                pass
        return {
            "credentials_file": str(_CREDS_PATH),
            "credentials_exists": creds_exists,
            "token_exists": token_exists,
            "authenticated": authenticated,
            "token_path": str(_TOKEN_PATH),
        }

    async def list_events(self, days: int = 7, max_results: int = 20,
                          calendar_id: str = "primary") -> dict:
        def _fetch():
            svc = _get_service()
            now = datetime.now(timezone.utc)
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days)).isoformat()
            result = (
                svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            items = result.get("items", [])
            events = []
            for e in items:
                start = e.get("start", {})
                end   = e.get("end", {})
                events.append({
                    "id":          e.get("id"),
                    "title":       e.get("summary", "(no title)"),
                    "description": e.get("description", ""),
                    "start":       start.get("dateTime", start.get("date", "")),
                    "end":         end.get("dateTime", end.get("date", "")),
                    "location":    e.get("location", ""),
                    "attendees":   [a.get("email") for a in e.get("attendees", [])],
                    "html_link":   e.get("htmlLink", ""),
                })
            return {"events": events, "count": len(events), "days": days}
        try:
            return await self._loop_run(_fetch)
        except Exception as e:
            log.error(f"[CalendarAgent] list_events failed: {e}")
            return {"error": str(e)}

    async def create_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        attendees: Optional[list[str]] = None,
        calendar_id: str = "primary",
        all_day: bool = False,
    ) -> dict:
        def _create():
            svc = _get_service()
            if all_day:
                body = {
                    "summary":     title,
                    "description": description,
                    "location":    location,
                    "start":       {"date": start[:10]},
                    "end":         {"date": end[:10]},
                }
            else:
                body = {
                    "summary":     title,
                    "description": description,
                    "location":    location,
                    "start":       {"dateTime": start, "timeZone": "UTC"},
                    "end":         {"dateTime": end,   "timeZone": "UTC"},
                }
            if attendees:
                body["attendees"] = [{"email": a} for a in attendees]
            event = svc.events().insert(calendarId=calendar_id, body=body).execute()
            return {
                "created": True,
                "id":      event.get("id"),
                "title":   event.get("summary"),
                "link":    event.get("htmlLink"),
                "start":   start,
                "end":     end,
            }
        try:
            return await self._loop_run(_create)
        except Exception as e:
            log.error(f"[CalendarAgent] create_event failed: {e}")
            return {"error": str(e)}

    async def update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        **kwargs,
    ) -> dict:
        def _update():
            svc = _get_service()
            event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
            if "title" in kwargs:
                event["summary"] = kwargs["title"]
            if "description" in kwargs:
                event["description"] = kwargs["description"]
            if "location" in kwargs:
                event["location"] = kwargs["location"]
            if "start" in kwargs:
                event["start"] = {"dateTime": kwargs["start"], "timeZone": "UTC"}
            if "end" in kwargs:
                event["end"] = {"dateTime": kwargs["end"], "timeZone": "UTC"}
            updated = svc.events().update(
                calendarId=calendar_id, eventId=event_id, body=event
            ).execute()
            return {"updated": True, "id": updated.get("id"), "link": updated.get("htmlLink")}
        try:
            return await self._loop_run(_update)
        except Exception as e:
            log.error(f"[CalendarAgent] update_event failed: {e}")
            return {"error": str(e)}

    async def delete_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        def _delete():
            svc = _get_service()
            svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            return {"deleted": True, "id": event_id}
        try:
            return await self._loop_run(_delete)
        except Exception as e:
            log.error(f"[CalendarAgent] delete_event failed: {e}")
            return {"error": str(e)}

    async def find_free_slots(
        self,
        date: str,
        duration_minutes: int = 30,
        work_start: int = 9,
        work_end: int = 18,
        calendar_id: str = "primary",
    ) -> dict:
        """Find free time slots on a given date (YYYY-MM-DD) within working hours."""
        def _find():
            svc = _get_service()
            day_start = datetime.fromisoformat(f"{date}T{work_start:02d}:00:00+00:00")
            day_end   = datetime.fromisoformat(f"{date}T{work_end:02d}:00:00+00:00")

            result = (
                svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=day_start.isoformat(),
                    timeMax=day_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            items = result.get("items", [])
            busy: list[tuple[datetime, datetime]] = []
            for e in items:
                s = e.get("start", {}).get("dateTime")
                n = e.get("end",   {}).get("dateTime")
                if s and n:
                    busy.append((datetime.fromisoformat(s), datetime.fromisoformat(n)))

            # Walk the day finding gaps >= duration_minutes
            slots = []
            cursor = day_start
            delta  = timedelta(minutes=duration_minutes)
            for bs, be in sorted(busy):
                if cursor + delta <= bs:
                    slots.append({
                        "start": cursor.isoformat(),
                        "end":   bs.isoformat(),
                        "duration_minutes": int((bs - cursor).total_seconds() / 60),
                    })
                cursor = max(cursor, be)
            if cursor + delta <= day_end:
                slots.append({
                    "start": cursor.isoformat(),
                    "end":   day_end.isoformat(),
                    "duration_minutes": int((day_end - cursor).total_seconds() / 60),
                })
            return {"date": date, "free_slots": slots, "duration_requested": duration_minutes}
        try:
            return await self._loop_run(_find)
        except Exception as e:
            log.error(f"[CalendarAgent] find_free_slots failed: {e}")
            return {"error": str(e)}

    async def list_calendars(self) -> dict:
        def _list():
            svc = _get_service()
            result = svc.calendarList().list().execute()
            cals = [{"id": c.get("id"), "name": c.get("summary"), "primary": c.get("primary", False)}
                    for c in result.get("items", [])]
            return {"calendars": cals}
        try:
            return await self._loop_run(_list)
        except Exception as e:
            return {"error": str(e)}
