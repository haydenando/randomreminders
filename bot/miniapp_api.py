"""FastAPI router for the Skeddy-style mini app.

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

class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    kind: str = "reminder"  # reminder | task | task_no_notify
    schedule_type: str = "interval"  # once|every_day|interval|weekdays|monthdays|daterange
    tz: str = "UTC"
    image_id: str | None = None
    image_file_id: str | None = None  # legacy (pre-upload) field
    message_template: str | None = None
    note: str | None = None
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
    items.sort(key=lambda r: r.next_fire or "9999")
    out = []
    for r in items:
        d = r.to_dict()
        d["next_fire_local"] = _fmt_next(r, us_tz(user_id)) if r.next_fire else None
        d["line"] = _render_reminder_line(r, us_tz(user_id))
        out.append(d)
    return {"items": out, "count": len(out)}


def us_tz(user_id: int) -> str:
    if STATE.settings is None or STATE.vault is None:
        return "UTC"
    us = STATE.vault.db.user(user_id)
    return us.timezone or STATE.settings.default_tz


@router.post("/reminders", status_code=201)
async def create_reminder(body: ReminderIn, user_id: int = Depends(_user_id_from_query)):
    if STATE.settings is None or STATE.vault is None or STATE.sched is None:
        raise HTTPException(503, "not ready")
    if not STATE.vault.db.user(user_id).verified:
        raise HTTPException(401, "not verified")
    r = Reminder(
        id=STATE.vault.db.next_id(),
        user_id=user_id,
        text=body.text,
        kind=body.kind,
        schedule_type=body.schedule_type,
        at=body.at,
        time_of_day=body.time_of_day,
        min_seconds=body.min_seconds,
        max_seconds=body.max_seconds,
        weekdays=body.weekdays,
        monthdays=body.monthdays,
        range_start=body.range_start,
        range_end=body.range_end,
        tz=body.tz or us_tz(user_id),
        image_id=body.image_id,
        image_file_id=body.image_file_id,
        message_template=body.message_template,
        note=body.note,
    )
    await STATE.sched.add(r)
    d = r.to_dict()
    d["next_fire_local"] = _fmt_next(r, us_tz(user_id)) if r.next_fire else None
    return d


@router.patch("/reminders/{rid}")
async def update_reminder(rid: int, body: ReminderIn, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    r = STATE.vault.db.reminders.get(rid)
    if r is None or r.user_id != user_id:
        raise HTTPException(404, "not found")
    r.text = body.text
    r.kind = body.kind
    r.schedule_type = body.schedule_type
    r.at = body.at
    r.time_of_day = body.time_of_day
    r.min_seconds = body.min_seconds
    r.max_seconds = body.max_seconds
    r.weekdays = body.weekdays
    r.monthdays = body.monthdays
    r.range_start = body.range_start
    r.range_end = body.range_end
    r.tz = body.tz or r.tz
    r.image_id = body.image_id
    r.image_file_id = body.image_file_id
    r.message_template = body.message_template
    r.note = body.note
    await STATE.sched.update(r)
    d = r.to_dict()
    d["next_fire_local"] = _fmt_next(r, us_tz(user_id)) if r.next_fire else None
    return d


@router.delete("/reminders/{rid}")
async def delete_reminder(rid: int, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    r = await STATE.sched.remove(rid)
    if r is None or r.user_id != user_id:
        raise HTTPException(404, "not found")
    return {"ok": True}


@router.post("/reminders/{rid}/done")
async def mark_done(rid: int, user_id: int = Depends(_user_id_from_query)):
    if STATE.sched is None:
        raise HTTPException(503, "not ready")
    r = STATE.vault.db.reminders.get(rid)
    if r is None or r.user_id != user_id:
        raise HTTPException(404, "not found")
    r.completed = True
    if r.kind == "task":
        await STATE.sched.remove(rid)
    else:
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
