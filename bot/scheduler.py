"""Scheduling logic.

Wraps APScheduler to fire reminders. The key feature: interval schedules
with a [min, max] range roll a new random delay each time the reminder
fires, so the gap between pings is truly uniform in that window.

Schedule kinds and how the next fire time is computed:
  once        -> next_fire = at
  every_day   -> next_fire = next HH:MM in user's tz
  interval    -> next_fire = now + uniform(min_seconds, max_seconds)
  weekdays    -> next_fire = next HH:MM on a selected weekday
  monthdays   -> next_fire = next HH:MM on a selected day-of-month
  daterange   -> next_fire = next HH:MM while today is in [start, end]
"""
from __future__ import annotations

import asyncio

import logging
import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from .config import Reminder, Settings, utcnow
from .storage import DB, Vault

log = logging.getLogger(__name__)


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _parse_hhmm(s: str | None, default: time = time(9, 0)) -> time:
    if not s:
        return default
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        return default


def _next_daily(tod: time, tz_name: str, after: datetime) -> datetime:
    """Next occurrence of tod in tz_name, strictly after `after` (UTC)."""
    z = _tz(tz_name)
    local_after = after.astimezone(z)
    cand = datetime.combine(local_after.date(), tod, tzinfo=z)
    if cand <= after:
        cand = cand + timedelta(days=1)
    return cand.astimezone(timezone.utc)


def _next_weekday(tod: time, tz_name: str, weekdays: list[int], after: datetime) -> datetime | None:
    """Next occurrence of tod on a selected weekday (0=Mon..6=Sun)."""
    if not weekdays:
        return None
    z = _tz(tz_name)
    local_after = after.astimezone(z)
    for offset in range(0, 8):
        day = local_after.date() + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        cand = datetime.combine(day, tod, tzinfo=z)
        if offset == 0 and cand <= after:
            cand = cand + timedelta(days=7)
            # re-check: a later weekday might be sooner
            continue
        return cand.astimezone(timezone.utc)
    # fallback: search a bit further
    for offset in range(0, 15):
        day = local_after.date() + timedelta(days=offset)
        if day.weekday() in weekdays:
            cand = datetime.combine(day, tod, tzinfo=z)
            if cand > after:
                return cand.astimezone(timezone.utc)
    return None


def _next_monthday(tod: time, tz_name: str, monthdays: list[int], after: datetime) -> datetime | None:
    """Next occurrence of tod on a selected day-of-month (1..31)."""
    if not monthdays:
        return None
    z = _tz(tz_name)
    local_after = after.astimezone(z)
    for offset in range(0, 400):
        day = local_after.date() + timedelta(days=offset)
        if day.day not in monthdays:
            continue
        cand = datetime.combine(day, tod, tzinfo=z)
        if cand > after:
            return cand.astimezone(timezone.utc)
    return None


def _next_in_range(
    tod: time, tz_name: str, start: str, end: str, after: datetime
) -> datetime | None:
    """Next occurrence of tod on a date within [start, end]."""
    if not start or not end:
        return None
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return None
    z = _tz(tz_name)
    local_after = after.astimezone(z)
    day = local_after.date()
    while day <= end_d:
        if day >= start_d:
            cand = datetime.combine(day, tod, tzinfo=z)
            if cand > after:
                return cand.astimezone(timezone.utc)
        day = day + timedelta(days=1)
    return None


def compute_next_fire(r: Reminder, from_dt: datetime) -> datetime | None:
    """Compute the next fire time for a reminder, given the current time."""
    # Interval schedules run indefinitely and are never auto-completed
    if r.schedule_type == "interval":
        r.completed = False
    elif r.completed:
        return None
    tod = _parse_hhmm(r.time_of_day)
    if r.schedule_type == "once":
        if r.at:
            cand = datetime.fromisoformat(r.at)
            if cand.tzinfo is None:
                cand = cand.replace(tzinfo=timezone.utc)
            return cand if cand > from_dt else None
        return None
    if r.schedule_type == "every_day":
        return _next_daily(tod, r.tz, from_dt)
    if r.schedule_type == "interval":
        lo = float(r.min_seconds or 60)
        hi = float(r.max_seconds or lo)
        if hi < lo:
            hi = lo
        delay = random.uniform(lo, hi)
        return from_dt + timedelta(seconds=delay)
    if r.schedule_type == "weekdays":
        return _next_weekday(tod, r.tz, r.weekdays, from_dt)
    if r.schedule_type == "monthdays":
        return _next_monthday(tod, r.tz, r.monthdays, from_dt)
    if r.schedule_type == "daterange":
        return _next_in_range(tod, r.tz, r.range_start, r.range_end, from_dt)
    return None


class ReminderScheduler:
    def __init__(self, settings: Settings, vault: Vault, send_fn):
        self.settings = settings
        self.vault = vault
        self.send_fn = send_fn  # async (user_id, reminder) -> None
        self.sched = AsyncIOScheduler(timezone=utcnow().astimezone().tzinfo)
        self._watchdog_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Load state, schedule everything, start the APScheduler loop."""
        await self.vault.setup()
        self._reschedule_all()
        self.sched.start()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        log.info("Scheduler started with %d reminders (watchdog active)", len(self.vault.db.reminders))

    async def shutdown(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
        try:
            self.sched.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                now = utcnow()
                for r in list(self.vault.db.reminders.values()):
                    if r.completed:
                        continue
                    if r.next_fire:
                        try:
                            nxt = datetime.fromisoformat(r.next_fire)
                            if nxt.tzinfo is None:
                                nxt = nxt.replace(tzinfo=timezone.utc)
                            if nxt <= now:
                                log.info("Watchdog firing overdue reminder #%s", r.id)
                                await self._fire(r.id)
                        except Exception:
                            pass
                    else:
                        self._reschedule_one(r, now)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Watchdog error")

    def _job_id(self, rid: int) -> str:
        return f"reminder-{rid}"

    def _reschedule_all(self) -> None:
        now = utcnow()
        for r in list(self.vault.db.reminders.values()):
            if r.completed:
                continue
            self._reschedule_one(r, now)

    def _reschedule_one(self, r: Reminder, now: datetime) -> None:
        jid = self._job_id(r.id)
        # remove any existing job for this reminder
        try:
            self.sched.remove_job(jid)
        except Exception:  # noqa: BLE001
            pass
        nxt = compute_next_fire(r, now)
        if nxt is None:
            # no more fires (e.g. one-shot already passed, or daterange over)
            if r.schedule_type == "once" and not r.completed:
                r.completed = True
            return
        r.next_fire = nxt.isoformat()
        self.sched.add_job(
            self._fire,
            trigger=DateTrigger(run_date=nxt),
            id=jid,
            args=[r.id],
            replace_existing=True,
            misfire_grace_time=3600,
        )

    async def _fire(self, rid: int) -> None:
        db = self.vault.db
        r = db.reminders.get(rid)
        if r is None or r.completed:
            return
        now = utcnow()
        r.last_fire = now.isoformat()
        if r.schedule_type == "once":
            r.completed = True
        try:
            await self.send_fn(r.user_id, r)
        except Exception:  # noqa: BLE001
            log.exception("Failed to send reminder #%s", rid)
        # For task_no_notify we never send; just mark last_fire.
        # Reschedule for next fire (interval re-rolls its random delay here).
        self._reschedule_one(r, now)
        try:
            await self.vault.save()
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist after fire")

    # ---- mutation helpers -------------------------------------------------

    async def add(self, r: Reminder) -> None:
        self.vault.db.reminders[r.id] = r
        self._reschedule_one(r, utcnow())
        await self.vault.save()

    async def update(self, r: Reminder) -> None:
        self.vault.db.reminders[r.id] = r
        self._reschedule_one(r, utcnow())
        await self.vault.save()

    async def remove(self, rid: int) -> Reminder | None:
        r = self.vault.db.reminders.pop(rid, None)
        if r is not None:
            try:
                self.sched.remove_job(self._job_id(rid))
            except Exception:  # noqa: BLE001
                pass
            if r.image_id:
                try:
                    await self.vault.delete_image(r.image_id)
                except Exception:  # noqa: BLE001
                    pass
            await self.vault.save()
        return r

    async def reschedule(self, rid: int) -> None:
        r = self.vault.db.reminders.get(rid)
        if r is not None:
            self._reschedule_one(r, utcnow())
            await self.vault.save()
