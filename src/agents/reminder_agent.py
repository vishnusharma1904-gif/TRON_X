"""
TRON-X Reminder Agent  (Phase 11)
------------------------------------
Windows toast notifications via winotify + APScheduler for timed delivery.
Falls back to a console log if winotify is unavailable (non-Windows).

Reminder store is in-memory (persists as long as server runs).
For persistence across restarts, reminders are also saved to
~/.tronx/reminders.json and reloaded on startup.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.core.logger import log

_STORE_PATH = Path.home() / ".tronx" / "reminders.json"


def _toast(title: str, message: str, icon_path: str = "") -> None:
    """Fire a Windows toast notification (or log on non-Windows)."""
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="TRON-X",
            title=title,
            msg=message,
            icon=icon_path or "",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        log.info(f"[reminder] Toast shown: {title}")
    except ImportError:
        # Auto-install winotify on Windows
        import sys, subprocess
        if sys.platform == "win32":
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "winotify",
                     "--break-system-packages", "--quiet"],
                    check=True,
                )
                from winotify import Notification, audio
                toast = Notification(app_id="TRON-X", title=title, msg=message)
                toast.set_audio(audio.Default, loop=False)
                toast.show()
                return
            except Exception as e:
                log.warning(f"[reminder] winotify install failed: {e}")
        log.info(f"[reminder] NOTIFY: [{title}] {message}")
    except Exception as e:
        log.error(f"[reminder] Toast failed: {e}")


class ReminderAgent:
    """In-memory reminder registry backed by APScheduler for timed delivery."""

    def __init__(self):
        self._reminders: dict[str, dict] = {}
        self._scheduler = None
        self._load_store()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_store(self):
        try:
            if _STORE_PATH.exists():
                data = json.loads(_STORE_PATH.read_text())
                # Only keep future reminders
                now = datetime.now(timezone.utc).isoformat()
                self._reminders = {
                    k: v for k, v in data.items()
                    if v.get("fire_at", "") > now and v.get("status") == "pending"
                }
                log.info(f"[reminder] Loaded {len(self._reminders)} pending reminders from disk")
        except Exception as e:
            log.warning(f"[reminder] Could not load reminder store: {e}")

    def _save_store(self):
        try:
            _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STORE_PATH.write_text(json.dumps(self._reminders, indent=2))
        except Exception as e:
            log.warning(f"[reminder] Could not save reminder store: {e}")

    # ------------------------------------------------------------------
    # Scheduler bootstrap
    # ------------------------------------------------------------------
    def _get_scheduler(self):
        from src.agents.scheduler_agent import get_scheduler
        return get_scheduler()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------
    async def set_reminder(
        self,
        message: str,
        fire_at: Optional[str] = None,
        delay_seconds: Optional[int] = None,
        title: str = "TRON-X Reminder",
        reminder_id: Optional[str] = None,
    ) -> dict:
        """
        Schedule a reminder.
        Provide either fire_at (ISO datetime string) or delay_seconds.
        """
        if fire_at is None and delay_seconds is None:
            return {"error": "Provide either fire_at or delay_seconds"}

        rid = reminder_id or str(uuid.uuid4())[:8]

        if fire_at is None:
            fire_dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            fire_at = fire_dt.isoformat()
        else:
            try:
                fire_dt = datetime.fromisoformat(fire_at)
                if fire_dt.tzinfo is None:
                    fire_dt = fire_dt.replace(tzinfo=timezone.utc)
                    fire_at = fire_dt.isoformat()
            except ValueError as e:
                return {"error": f"Invalid fire_at datetime: {e}"}

        record = {
            "id":       rid,
            "title":    title,
            "message":  message,
            "fire_at":  fire_at,
            "status":   "pending",
            "created":  datetime.now(timezone.utc).isoformat(),
        }
        self._reminders[rid] = record
        self._save_store()

        # Schedule the toast
        delay = max(0, (fire_dt - datetime.now(timezone.utc)).total_seconds())

        async def _fire_async():
            await asyncio.sleep(delay)
            await asyncio.get_event_loop().run_in_executor(
                None, _toast, title, message
            )
            if rid in self._reminders:
                self._reminders[rid]["status"] = "fired"
                self._save_store()

        asyncio.create_task(_fire_async())
        log.info(f"[reminder] Scheduled '{rid}' in {delay:.0f}s: {message[:60]}")

        return {
            "scheduled": True,
            "id":        rid,
            "fire_at":   fire_at,
            "message":   message,
            "delay_seconds": int(delay),
        }

    async def set_reminder_nl(self, message: str, when_nl: str, title: str = "TRON-X Reminder") -> dict:
        """Parse a natural-language time expression and schedule reminder."""
        try:
            from src.agents.scheduler_agent import get_scheduler
            sched = get_scheduler()
            parsed = await sched.parse_nl_schedule(when_nl)
            delay = parsed.get("delay_seconds") or parsed.get("seconds", 60)
            return await self.set_reminder(message=message, delay_seconds=int(delay), title=title)
        except Exception as e:
            log.warning(f"[reminder] NL parse failed ({e}), defaulting to 60s")
            return await self.set_reminder(message=message, delay_seconds=60, title=title)

    async def list_reminders(self, include_fired: bool = False) -> dict:
        reminders = list(self._reminders.values())
        if not include_fired:
            reminders = [r for r in reminders if r.get("status") == "pending"]
        reminders.sort(key=lambda r: r.get("fire_at", ""))
        return {"reminders": reminders, "count": len(reminders)}

    async def cancel_reminder(self, reminder_id: str) -> dict:
        if reminder_id not in self._reminders:
            return {"error": f"Reminder '{reminder_id}' not found"}
        self._reminders[reminder_id]["status"] = "cancelled"
        self._save_store()
        return {"cancelled": True, "id": reminder_id}

    async def fire_now(self, reminder_id: str) -> dict:
        """Immediately fire a reminder (test/manual trigger)."""
        rec = self._reminders.get(reminder_id)
        if not rec:
            return {"error": f"Reminder '{reminder_id}' not found"}
        await asyncio.get_event_loop().run_in_executor(
            None, _toast, rec["title"], rec["message"]
        )
        self._reminders[reminder_id]["status"] = "fired"
        self._save_store()
        return {"fired": True, "id": reminder_id}


# Module-level singleton
_reminder_agent: Optional[ReminderAgent] = None


def get_reminder_agent() -> ReminderAgent:
    global _reminder_agent
    if _reminder_agent is None:
        _reminder_agent = ReminderAgent()
    return _reminder_agent
