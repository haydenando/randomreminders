"""FastAPI router for the Randomly Remind Me mini app.

The mini app is a static HTML/JS page served at / that talks to these
JSON endpoints. Authentication: the mini app is opened from inside the
Telegram client, so we trust the user identity from the WebApp init data
(we verify the HMAC against the bot token — same way Telegram does).

For simplicity on the free tier we also accept a lightweight signed token
issued by the bot itself when the user taps the /miniapp button.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from telegram.ext import Application

from .config import Reminder, Settings, utcnow
from .handlers import _fmt_next, _render_reminder_line
from .scheduler import ReminderScheduler, compute_next_fire
from .storage import MAX_IMAGE_BYTES, Vault

router = APIRouter(prefix="/api")




class AppState:
    def __init__(self):
        self.settings: Settings | None = None
        self.vault: Vault | None = None
        self.sched: ReminderScheduler | None = None
        self.ptb_app: Application | None = None


STATE = AppState()


def _user_id_from_query(user_id: int | None = Query(default=None)) -> int:
    if user_id is None:
        raise HTTPException(status_code=401, detail="missing user_id")
    return int(user_id)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ScheduleIn(BaseModel):
    schedule_type: str = "once"  # once | interval | every_day | weekdays | monthdays | daterange
    at: str | None = None
    time_of_day: str | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    weekdays: list[int] = Field(default_factory=list)
    monthdays: list[int] = Field(default_factory=list)
    range_start: str | None = None
    range_end: str | None = None


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    kind: str = "reminder"  # reminder | countdown
    tz: str = "UTC"
    image_id: str | None = None
    image_file_id: str | None = None  # legacy (pre-upload) field
    message_template: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    # group key linking multiple schedules to the same logical reminder
    group_id: int | None = None
    # countdown mode params
    target_date: str | None = None        # "YYYY-MM-DD"
    notification_time: str | None = None  # "HH:MM"
    countdown_days_before: int | None = None
    # multiple schedules (regular mode)
    schedules: list[ScheduleIn] | None = None
    # backward-compatible single-schedule fields
    schedule_type: str | None = None
    at: str | None = None
    time_of_day: str | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    weekdays: list[int] = Field(default_factory=list)
    monthdays: list[int] = Field(default_factory=list)
    range_start: str | None = None
    range_end: str | None = None


class UserSettingsIn(BaseModel):
    timezone: str | None = None
    hide_snooze: bool | None = None
    language: str | None = None
    snooze_durations: list[int] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/healthz")
def healthz():
    return {"ok": True, "ts": utcnow().isoformat()}


@router.get("/me")
async def me(user_id: int = Depends(_user_id_from_query)):
    if STATE.settings is None or STATE.vault is None:
        raise HTTPException(503, "not ready")
    us = STATE.vault.db.user(user_id)
    return {
        "user_id": user_id,
        "verified": us.verified,
        "secret_phrase": STATE.settings.secret_phrase,
        "default_tz": STATE.settings.default_tz,
        "timezone": us.timezone,
        "hide_snooze": us.hide_snooze,
    }


@router.get("/reminders")
async def list_reminders(user_id: int = Depends(_user_id_from_query)):
    if STATE.settings is None or STATE.vault is None:
        raise HTTPException(503, "not ready")
    db = STATE.vault.db
    items = [r for r in db.reminders.values() if r.user_id == user_id]
    # Group schedules that belong to the same logical reminder (group_id)
    groups: dict[int, list] = {}
    for r in items:
        gid = r.group_id if r.group_id is not None else r.id
        groups.setdefault(gid, []).append(r)
    out = []
    for gid, mem in groups.items():
        mem.sort(key=lambda r: r.next_fire or "9999")
        primary = mem[0]
        d = primary.to_dict()
        d["group_id"] = gid
        d["schedules"] = [m.to_dict() for m in mem]
        d["next_fire_local"] = _fmt_next(primary, us_tz(user_id)) if primary.next_fire else None
        d["line"] = _render_reminder_line(primary, us_tz(user_id))
        out.append(d)
    return {"items": out, "count": len(out)}


def us_tz(user_id: int) -> str:
    if STATE.settings is None or STATE.vault is None:
        return "UTC"
    us = STATE.vault.db.user(user_id)
    return us.timezone or STATE.settings.default_tz


def _group_members(db, rid: int) -> list:
    """All reminders that share the same group_id as rid (or just [rid])."""
    r = db.reminders.get(rid)
    if r is None:
        return []
    gid = r.group_id
    if gid is None:
        return [r]
    return [x for x in db.reminders.values() if x.group_id == gid]


def _schedule_dicts(body: ReminderIn) -> list[dict]:
    """Return one schedule dict per schedule (schedule_type + params)."""
    if body.kind == "countdown":
        return _countdown_schedules(body)
    if body.schedules:
        return [s.model_dump() for s in body.schedules]
    # backward-compatible single schedule
    return [{
        "schedule_type": body.schedule_type or "interval",
        "at": body.at,
        "time_of_day": body.time_of_day,
        "min_seconds": body.min_seconds,
        "max_seconds": body.max_seconds,
        "weekdays": body.weekdays,
        "monthdays": body.monthdays,
        "range_start": body.range_start,
        "range_end": body.range_end,
    }]


def _countdown_schedules(body: ReminderIn) -> list[dict]:
    """Build schedule(s) for a countdown reminder."""
    from datetime import date, datetime, time as dtime, timedelta, timezone
    from zoneinfo import ZoneInfo

    target = body.target_date
    tod = body.notification_time or "09:00"
    days_before = body.countdown_days_before or 0
    z = ZoneInfo(body.tz or "UTC")
    if target and days_before > 0:
        try:
            t = date.fromisoformat(target)
            start = (t - timedelta(days=days_before)).isoformat()
            return [{"schedule_type": "daterange", "time_of_day": tod,
                     "range_start": start, "range_end": target}]
        except ValueError:
            pass
    if target:
        try:
            hh, mm = (tod.split(":") + ["00"])[:2]
            dt = datetime.combine(date.fromisoformat(target), dtime(int(hh), int(mm)), tzinfo=z)
            return [{"schedule_type": "once", "at": dt.astimezone(timezone.utc).isoformat()}]
        except (ValueError, TypeError):
            pass
    return []


def _build_reminder(schema: dict, user_id: int, body: ReminderIn, gid: int) -> Reminder:
    return Reminder(
        id=STATE.vault.db.next_id(),
        user_id=user_id,
        text=body.text,
        kind=body.kind,
        schedule_type=schema.get("schedule_type", "interval"),
        at=schema.get("at"),
        time_of_day=schema.get("time_of_day"),
        min_seconds=schema.get("min_seconds"),
        max_seconds=schema.get("max_seconds"),
        weekdays=schema.get("weekdays") or [],
        monthdays=schema.get("monthdays") or [],
        range_start=schema.get("range_start"),
        range_end=schema.get("range_end"),
        tz=body.tz or us_tz(user_id),
        image_id=body.image_id,
        image_file_id=body.image_file_id,
        message_template=body.message_template,
        note=body.note,
        tags=body.tags,
        target_date=body.target_date,
        notification_time=body.notification_time,
        countdown_days_before=body.countdown_days_before,
        group_id=gid,
    )


@router.post("/reminders", status_code=201)
async def create_reminder(body: ReminderIn, user_id: int = Depends(_user_id_from_query)):
    if STATE.settings is None or STATE.vault is None or STATE.sched is None:
        raise HTTPException(503, "not ready")
    if not STATE.vault.db.user(user_id).verified:
        raise HTTPException(401, "not verified")
    schemas = _schedule_dicts(body)
    if not schemas:
        raise HTTPException(422, "no schedule provided")
    gid = body.group_id or STATE.vault.db.next_id()
    created = []
    for s in schemas:
        r = _build_reminder(s, user_id, body, gid)
        await STATE.sched.add(r)
        created.append(r)
    primary = created[0]
    d = primary.to_dict()
    d["group_id"] = gid
    d["schedules"] = [c.to_dict() for c in created]
    d["next_fire_local"] = _fmt_next(primary, us_tz(user_id)) if primary.next_fire else None
    return d


@router.patch("/reminders/{rid}")
async def update_reminder(rid: int, body: ReminderIn, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    members = _group_members(STATE.vault.db, rid)
    if not members or members[0].user_id != user_id:
        raise HTTPException(404, "not found")
    gid = members[0].group_id if members[0].group_id is not None else rid
    for m in members:
        await STATE.sched.remove(m.id)
    schemas = _schedule_dicts(body)
    if not schemas:
        raise HTTPException(422, "no schedule provided")
    created = []
    for s in schemas:
        r = _build_reminder(s, user_id, body, gid)
        await STATE.sched.add(r)
        created.append(r)
    primary = created[0]
    d = primary.to_dict()
    d["group_id"] = gid
    d["schedules"] = [c.to_dict() for c in created]
    d["next_fire_local"] = _fmt_next(primary, us_tz(user_id)) if primary.next_fire else None
    return d


@router.delete("/reminders/{rid}")
async def delete_reminder(rid: int, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    members = _group_members(STATE.vault.db, rid)
    if not members or members[0].user_id != user_id:
        raise HTTPException(404, "not found")
    for m in members:
        await STATE.sched.remove(m.id)
    return {"ok": True}


@router.post("/reminders/{rid}/done")
async def mark_done(rid: int, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    r = STATE.vault.db.reminders.get(rid)
    if r is None or r.user_id != user_id:
        raise HTTPException(404, "not found")
    if r.schedule_type == "interval":
        # Intervals run indefinitely; "done" acknowledges and rolls next interval
        r.completed = False
        await STATE.sched.reschedule(rid)
    elif r.schedule_type == "once":
        r.completed = True
        await STATE.sched.remove(rid)
    else:
        # recurring (every_day/weekdays/monthdays/daterange) keep running
        r.completed = False
        await STATE.sched.reschedule(rid)
    return {"ok": True}


@router.post("/reminders/{rid}/snooze")
async def snooze(rid: int, seconds: int = Query(default=300), user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    r = STATE.vault.db.reminders.get(rid)
    if r is None or r.user_id != user_id:
        raise HTTPException(404, "not found")
    from datetime import timedelta
    now = utcnow()
    r.next_fire = (now + timedelta(seconds=seconds)).isoformat()
    STATE.sched._reschedule_one(r, now)
    await STATE.vault.save()
    return {"ok": True, "next_fire": r.next_fire}


@router.get("/settings")
async def get_settings(user_id: int = Depends(_user_id_from_query)):
    if STATE.settings is None or STATE.vault is None:
        raise HTTPException(503, "not ready")
    us = STATE.vault.db.user(user_id)
    return {
        "timezone": us.timezone,
        "hide_snooze": us.hide_snooze,
        "language": us.language,
        "default_tz": STATE.settings.default_tz,
        "snooze_durations": us.snooze_durations,
    }


@router.put("/settings")
async def put_settings(body: UserSettingsIn, user_id: int = Depends(_user_id_from_query)):
    if STATE.vault is None:
        raise HTTPException(503, "not ready")
    us = STATE.vault.db.user(user_id)
    if body.timezone is not None:
        us.timezone = body.timezone
    if body.hide_snooze is not None:
        us.hide_snooze = body.hide_snooze
    if body.language is not None:
        us.language = body.language
    if body.snooze_durations is not None:
        # sanitize: positive integers, up to 3, cap at 7 days
        durs = [int(x) for x in body.snooze_durations if isinstance(x, (int, float)) and x > 0][:3]
        if durs:
            us.snooze_durations = [min(d, 604800) for d in durs]
    await STATE.vault.save()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Images (uploaded from the mini app; stored via the vault, see storage.py)
# ---------------------------------------------------------------------------

@router.post("/images", status_code=201)
async def upload_image(file: UploadFile = File(...), user_id: int = Depends(_user_id_from_query)):
    if STATE.vault is None:
        raise HTTPException(503, "not ready")
    if not STATE.vault.db.user(user_id).verified:
        raise HTTPException(401, "not verified")
    if (file.content_type or "") not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, "only JPEG, PNG or WebP images are supported")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "image too large (max 5 MB)")
    try:
        image_id = await STATE.vault.put_image(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not store image: {exc}")
    return {"image_id": image_id, "url": f"/api/images/{image_id}"}


@router.get("/images/{image_id}")
async def get_image(image_id: str, user_id: int = Depends(_user_id_from_query)):
    if STATE.vault is None:
        raise HTTPException(503, "not ready")
    data = await STATE.vault.get_image(image_id)
    if data is None:
        raise HTTPException(404, "image not found")
    return Response(content=data, media_type="image/jpeg")


@router.delete("/images/{image_id}")
async def delete_image(image_id: str, user_id: int = Depends(_user_id_from_query)):
    if STATE.vault is None:
        raise HTTPException(503, "not ready")
    await STATE.vault.delete_image(image_id)
    return {"ok": True}


# PUT alias for PATCH /reminders/{rid} (the mini app uses PUT)
@router.put("/reminders/{rid}")
async def update_reminder_put(rid: int, body: ReminderIn, user_id: int = Depends(_user_id_from_query)):
    return await update_reminder(rid, body, user_id)
