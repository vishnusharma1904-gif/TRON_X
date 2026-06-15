"""
TRON-X Scheduler Agent
────────────────────────
APScheduler-based proactive task runner.
Supports:
  - Cron jobs ("every morning at 8am")
  - Interval jobs ("every 30 minutes")
  - One-shot delayed jobs ("in 10 minutes")
  - NL schedule parsing via LLM
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.logger import log

_TZ = "UTC"


def _get_tz():
    try:
        return ZoneInfo(_TZ)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


class SchedulerAgent:
    """Wraps APScheduler AsyncIOScheduler for proactive TRON-X tasks."""

    def __init__(self):
        self._scheduler = None
        self._jobs: dict[str, dict] = {}

    def start(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            self._scheduler = AsyncIOScheduler(timezone=_get_tz())
            self._scheduler.start()
            log.info("[scheduler] APScheduler started")
        except ImportError:
            log.warning("[scheduler] apscheduler not installed: pip install apscheduler")

    def stop(self):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ── Job registration ──────────────────────────────────────────────────────

    def add_cron_job(
        self,
        job_id: str,
        func: Callable,
        cron_expr: str,
        args: Optional[list] = None,
        description: str = "",
    ) -> dict:
        """Add a cron job. cron_expr format: 'minute hour dom month dow'"""
        if not self._scheduler:
            return {"success": False, "error": "Scheduler not started"}

        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return {"success": False, "error": "Invalid cron (need 5 fields)"}

        minute, hour, dom, month, dow = parts

        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger(
            minute=minute, hour=hour,
            day=dom, month=month, day_of_week=dow,
            timezone=_get_tz(),
        )
        self._scheduler.add_job(
            func, trigger=trigger, id=job_id, args=args or [],
            replace_existing=True,
        )
        self._jobs[job_id] = {
            "id": job_id, "type": "cron", "expr": cron_expr,
            "description": description,
        }
        log.info(f"[scheduler] Cron job '{job_id}' → {cron_expr}")
        return {"success": True, "job_id": job_id}

    def add_interval_job(
        self,
        job_id: str,
        func: Callable,
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        args: Optional[list] = None,
        description: str = "",
    ) -> dict:
        if not self._scheduler:
            return {"success": False, "error": "Scheduler not started"}

        from apscheduler.triggers.interval import IntervalTrigger
        trigger = IntervalTrigger(seconds=seconds, minutes=minutes, hours=hours)
        self._scheduler.add_job(
            func, trigger=trigger, id=job_id, args=args or [],
            replace_existing=True,
        )
        self._jobs[job_id] = {
            "id": job_id, "type": "interval",
            "seconds": seconds + minutes * 60 + hours * 3600,
            "description": description,
        }
        log.info(f"[scheduler] Interval job '{job_id}' every {seconds + minutes*60 + hours*3600}s")
        return {"success": True, "job_id": job_id}

    def add_oneshot_job(
        self,
        job_id: str,
        func: Callable,
        delay_seconds: int,
        args: Optional[list] = None,
        description: str = "",
    ) -> dict:
        if not self._scheduler:
            return {"success": False, "error": "Scheduler not started"}

        run_at = datetime.now(_get_tz()) + timedelta(seconds=delay_seconds)
        from apscheduler.triggers.date import DateTrigger
        trigger = DateTrigger(run_date=run_at, timezone=_get_tz())
        self._scheduler.add_job(
            func, trigger=trigger, id=job_id, args=args or [],
            replace_existing=True,
        )
        self._jobs[job_id] = {
            "id": job_id, "type": "oneshot",
            "run_at": run_at.isoformat(),
            "description": description,
        }
        log.info(f"[scheduler] One-shot job '{job_id}' at {run_at.isoformat()}")
        return {"success": True, "job_id": job_id, "run_at": run_at.isoformat()}

    def remove_job(self, job_id: str) -> dict:
        if not self._scheduler:
            return {"success": False, "error": "Scheduler not started"}
        try:
            self._scheduler.remove_job(job_id)
            self._jobs.pop(job_id, None)
            return {"success": True, "removed": job_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_jobs(self) -> list[dict]:
        return list(self._jobs.values())

    # ── NL schedule parser ────────────────────────────────────────────────────

    async def parse_nl_schedule(self, text: str) -> dict:
        """
        Parse natural language schedule description into structured config.
        e.g. 'every morning at 8am' → {type:'cron', expr:'0 8 * * *'}
        """
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        prompt = (
            f"Parse this schedule: \"{text}\"\n"
            "Return JSON with one of:\n"
            '  {"type": "cron",     "expr": "0 8 * * *"}\n'
            '  {"type": "interval", "seconds": 1800}\n'
            '  {"type": "oneshot",  "delay_seconds": 600}\n'
            "Return ONLY the JSON, no markdown."
        )
        result = await orch.chat(
            prompt, "__scheduler__", "reasoning", "jarvis", max_tokens=80, temperature=0.0
        )
        import json, re
        try:
            clean = re.sub(r"```(?:json)?", "", result.get("reply", "")).strip()
            return json.loads(clean)
        except Exception:
            return {"type": "unknown", "raw": text}

    # ── Built-in proactive tasks ───────────────────────────────────────────────

    async def _daily_briefing(self, session_id: str = "__proactive__",
                               persona: str = "jarvis"):
        """Morning briefing: home summary + any pending tasks."""
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        home_ctx = ""
        try:
            from src.iot.home_assistant import get_ha
            home_ctx = await get_ha().home_summary()
        except Exception:
            pass

        prompt = (
            "Generate a concise morning briefing. Include:\n"
            "- Current time and date\n"
            "- Friendly greeting\n"
            "- Home status summary (if available)\n"
            f"{home_ctx[:500] if home_ctx else ''}\n"
            "- A motivational note\n"
            "Keep it under 100 words."
        )
        result = await orch.chat(prompt, session_id, "chat", persona, max_tokens=300)
        log.info(f"[scheduler] Daily briefing: {result.get('reply','')[:80]}…")
        return result.get("reply", "")

    def register_daily_briefing(self, hour: int = 8, persona: str = "jarvis"):
        """Schedule daily briefing at given hour."""

        async def _run():
            await self._daily_briefing(persona=persona)

        return self.add_cron_job(
            "daily_briefing", _run,
            cron_expr=f"0 {hour} * * *",
            description=f"Daily briefing at {hour:02d}:00",
        )


# ── Singleton ──────────────────────────────────────────────────────────────────

_scheduler: Optional[SchedulerAgent] = None


def get_scheduler() -> SchedulerAgent:
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerAgent()
    return _scheduler
