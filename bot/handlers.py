"""Telegram chat handlers.

All commands go through the secret-phrase gate on first use. Once verified,
the user gets a Flexible Reminder onboarding (mini app button or chat setup with
timezone), then creates reminders by just chatting.

Reminders are delivered as plain text (or photo+caption) messages with NO
inline buttons — completion, deletion and snoozing happen in the mini app.

Commands:
  /start               - onboarding / return to the main menu
  /list                - show reminders
  /delete <id>         - delete a reminder
  /help                - open help
  /formats             - supported time formats
  /help_reminder_types - difference between reminder and countdown
  /help_schedules      - what the schedule icons mean
  /cancel              - cancel the current inline action
  /miniapp             - open the Flexible Reminder mini app
  /settings            - open settings (timezone, snooze)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Reminder, Settings, utcnow, tz
from .scheduler import ReminderScheduler, compute_next_fire
from .storage import DB, UserState, Vault

log = logging.getLogger(__name__)

# Common time zones offered during onboarding / settings (grid buttons).
COMMON_TIMEZONES = [
    "Europe/Amsterdam", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "Asia/Singapore", "Asia/Hong_Kong", "Asia/Seoul", "Asia/Tokyo",
    "Australia/Sydney", "Pacific/Auckland", "UTC",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def settings(ctx: ContextTypes.DEFAULT_TYPE) -> Settings:
    return ctx.bot_data["settings"]


def vault(ctx: ContextTypes.DEFAULT_TYPE) -> Vault:
    return ctx.bot_data["vault"]


def sched(ctx: ContextTypes.DEFAULT_TYPE) -> ReminderScheduler:
    return ctx.bot_data["scheduler"]


def user_state(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> UserState:
    uid = update.effective_user.id
    return vault(ctx).db.user(uid)


def is_verified(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    return user_state(ctx, update).verified


async def verify_gate(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    """Return True if the user is verified, else send the prompt and False."""
    if is_verified(ctx, update):
        return True
    cfg = settings(ctx)
    msg = (
        "🔒 This bot is private.\n"
        "Type the secret phrase to continue:\n"
        f"<code>{cfg.secret_phrase}</code>"
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")
    # remember we've prompted so we don't nag on every command
    ctx.user_data["awaiting_phrase"] = True
    return False


def db_for(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> DB:
    return vault(ctx).db


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    if hours < 24:
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _fmt_next(r: Reminder, user_tz: str) -> str:
    if not r.next_fire:
        return "?"
    try:
        nxt = datetime.fromisoformat(r.next_fire)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        local = nxt.astimezone(tz(user_tz))
        delta = nxt - utcnow()
        return f"{local.strftime('%H:%M')} ({_fmt_duration(delta.total_seconds() if delta.total_seconds() >= 0 else 0)})"
    except Exception:  # noqa: BLE001
        return r.next_fire


def _schedule_icon(r: Reminder) -> str:
    return {
        "once": "📌",
        "every_day": "📅",
        "interval": "🎲",
        "weekdays": "🗓️",
        "monthdays": "📆",
        "daterange": "🔁",
        "every_ndays": "🔁",
        "cron": "⏰",
    }.get(r.schedule_type, "❓")


def _render_reminder_line(r: Reminder, user_tz: str) -> str:
    icon = _schedule_icon(r)
    kind_tag = {
        "reminder": "R",
        "countdown": "⏳",
    }.get(r.kind, "?")
    parts = [f"{icon} #{r.id} [{kind_tag}] {r.text}"]
    if r.next_fire:
        parts.append(f"· next: {_fmt_next(r, user_tz)}")
    if r.note:
        parts.append(f"· note: {r.note}")
    return "\n".join(parts)


def _miniapp_url() -> str | None:
    return os.getenv("MINI_APP_URL") or None


def _onboard_keyboard() -> InlineKeyboardMarkup:
    """First onboarding screen: mini app button + chat setup (Flexible Reminder)."""
    url = _miniapp_url()
    rows = [[InlineKeyboardButton("💬 Set up in chat", callback_data="setup:chat")]]
    if url:
        rows.insert(0, [InlineKeyboardButton("🚀 Open Mini App", web_app=WebAppInfo(url=url))])
    return InlineKeyboardMarkup(rows)


def _tz_short(name: str) -> str:
    return name.split("/", 1)[-1].replace("_", " ")


def _tz_keyboard(prefix: str = "tz") -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for name in COMMON_TIMEZONES:
        row.append(InlineKeyboardButton(_tz_short(name), callback_data=f"{prefix}:{name}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [InlineKeyboardButton("⌨️ Type another time zone", callback_data=f"{prefix}:other")]
    )
    return InlineKeyboardMarkup(buttons)


def _setup_chat_panel() -> tuple[str, InlineKeyboardMarkup]:
    """The 'Set up in chat' screen: time zone selection."""
    text = (
        "➡️ Select time zone\n"
        "This is the time zone I'll use to remind you.\n"
        "It can be changed later in /settings."
    )
    return text, _tz_keyboard("tz")


async def _finish_onboarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """'All set' message after the user finished chat setup."""
    us = user_state(ctx, update)
    us.onboarded = True
    await vault(ctx).save()
    tzname = us.timezone or settings(ctx).default_tz
    text = (
        f"All set ✅\n\n"
        f"⏰ Time zone: {tzname}\n\n"
        "Now tell me what you want to be reminded about, e.g.\n"
        "<code>every 10-20m stretch</code>\n"
        "or open the mini app for the full editor."
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def _send_welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome screen for a verified user (Flexible Reminder)."""
    us = user_state(ctx, update)
    if not us.onboarded:
        await update.effective_message.reply_text(
            "Let's get you set up 👋", reply_markup=_onboard_keyboard()
        )
        return
    tzname = us.timezone or settings(ctx).default_tz
    await update.effective_message.reply_text(
        f"All set ✅ Time zone: {tzname}\n"
        "Just tell me what to remind you about, or tap the button below.",
        reply_markup=_onboard_keyboard(),
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — start using the bot or return to the main menu."""
    if not is_verified(ctx, update):
        # First contact: ask for the secret phrase.
        ctx.user_data["awaiting_phrase"] = True
        await update.effective_message.reply_text(
            "🔒 This bot is private.\n"
            "Type the secret phrase to continue."
        )
        return
    ctx.user_data["awaiting_phrase"] = False
    await _send_welcome(update, ctx)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — show your reminders (plain text, no buttons)."""
    if not await verify_gate(ctx, update):
        return
    us = user_state(ctx, update)
    tzname = us.timezone or settings(ctx).default_tz
    reminders = [
        r for r in vault(ctx).db.reminders.values()
        if r.user_id == update.effective_user.id and not r.completed
    ]
    if not reminders:
        await update.effective_message.reply_text(
            "No active reminders. Create one via chat or the mini app."
        )
        return
    reminders.sort(key=lambda r: r.next_fire or "9999")
    lines = [f"📋 Your active reminders ({len(reminders)}):", ""]
    for r in reminders:
        lines.append(_render_reminder_line(r, tzname))
        lines.append("")
    lines.append("To delete: /delete <id> or use the mini app.")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/delete <id> — delete a reminder in real time."""
    if not await verify_gate(ctx, update):
        return
    args = ctx.args or []
    if not args or not args[0].lstrip("#").isdigit():
        await update.effective_message.reply_text("Usage: <code>/delete &lt;id&gt;</code> (e.g. <code>/delete 1</code>)", parse_mode="HTML")
        return
    rid = int(args[0].lstrip("#"))
    r = vault(ctx).db.reminders.get(rid)
    if r is None or r.user_id != update.effective_user.id:
        await update.effective_message.reply_text(f"❌ Reminder #{rid} not found.")
        return
    await sched(ctx).remove(rid)
    await update.effective_message.reply_text(f"🗑 Deleted reminder #{rid}: {r.text}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — open Help."""
    if not await verify_gate(ctx, update):
        return
    text = (
        "Flexible Reminder help\n\n"
        "Commands\n"
        "/start — main menu\n"
        "/list — your reminders\n"
        "/delete <id> — delete a reminder\n"
        "/formats — supported time formats\n"
        "/help_reminder_types — reminder vs task\n"
        "/help_schedules — schedule icons\n"
        "/cancel — cancel the current action\n"
        "/miniapp — open the app\n"
        "/settings — timezone & options\n\n"
        "Creating reminders\n"
        "Just send me a message like 'every 10-20m stretch', or use the"
        " mini app for the full editor (images, notes, schedules)."
    )
    await update.effective_message.reply_text(text)


async def cmd_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/formats — view supported date and time formats."""
    if not await verify_gate(ctx, update):
        return
    text = (
        "Schedule formats\n\n"
        "Intervals:\n"
        "  every 30m        — every 30 minutes\n"
        "  every 10-20m     — every 10 to 20 minutes (random)\n\n"
        "Every N days:\n"
        "  every 4d at 18:00                  — every 4 days at 6pm\n"
        "  every 4d at random                 — every 4 days, any time\n"
        "  every 4d at random 08:00-20:00     — every 4 days, random 8am-8pm\n"
        "  every 2 weeks at 09:00             — every 2 weeks at 9am\n\n"
        "Weekly / monthly:\n"
        "  every monday at 09:00\n"
        "  every weekday at 09:00\n"
        "  every day at 09:00\n\n"
        "Cron:\n"
        "  cron 0 9 * * *     — 5-field crontab\n\n"
        "One-off:\n"
        "  in 2h            — in 2 hours\n"
        "  tomorrow at 09:00\n"
        "  2026-05-01 14:00\n"
        "Separate several one-offs with · (e.g. \"in 2h · tomorrow at 09:00\")\n"
    )
    await update.effective_message.reply_text(text)


async def cmd_help_reminder_types(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help_reminder_types — the difference between reminders and countdowns."""
    if not await verify_gate(ctx, update):
        return
    text = (
        "Reminder types\n\n"
        "🔔 Reminder\n"
        "  I send it at the scheduled time(s).\n\n"
        "⏳ Countdown\n"
        "  I remind you leading up to a target date/time.\n"
    )
    await update.effective_message.reply_text(text)


async def cmd_help_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help_schedules — what the icons next to a reminder's times mean."""
    if not await verify_gate(ctx, update):
        return
    text = (
        "Schedule icons\n\n"
        "📌 Specific time — fire once at a set datetime\n"
        "📅 Every day — fire at HH:MM every day in your timezone\n"
        "🎲 Interval — fire every N seconds (or random in [min, max])\n"
        "🗓️  Days of the week — fire at HH:MM on selected weekdays\n"
        "📆 Days of the month — fire at HH:MM on selected day numbers\n"
        "🔁 Date range — fire at HH:MM every day while in [start, end]\n\n"
        "Reminder type tags: R = reminder, T = task, S = silent task"
    )
    await update.effective_message.reply_text(text)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — cancel the current action."""
    if not await verify_gate(ctx, update):
        return
    ctx.user_data["awaiting_phrase"] = False
    # also clear any in-progress callback state
    ctx.user_data.pop("pending_schedule", None)
    ctx.user_data.pop("pending_kind", None)
    ctx.user_data.pop("pending_text", None)
    ctx.user_data.pop("awaiting_tz", None)
    await update.effective_message.reply_text("Cancelled. Use /start to begin again.")


async def cmd_miniapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/miniapp — open the app."""
    if not await verify_gate(ctx, update):
        return
    app_url = _miniapp_url()
    if not app_url:
        await update.effective_message.reply_text(
            "Mini app is not configured yet. Set the MINI_APP_URL environment "
            "variable to your Render deploy URL (e.g. https://randomreminders.onrender.com)."
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Open Mini App", web_app=WebAppInfo(url=app_url))]]
    )
    await update.effective_message.reply_text("Opening Flexible Reminder mini app:", reply_markup=kb)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings — open the settings (Flexible Reminder panel)."""
    if not await verify_gate(ctx, update):
        return
    us = user_state(ctx, update)
    kb_rows = [
        [
            InlineKeyboardButton("➡️ Time zone", callback_data="set:tz"),
        ],
        [
            InlineKeyboardButton(
                f"Snooze buttons: {'hidden' if us.hide_snooze else 'visible'}",
                callback_data="set:snooze",
            ),
        ],
    ]
    app_url = _miniapp_url()
    if app_url:
        kb_rows.append([InlineKeyboardButton("⚙️ All settings in mini app", url=app_url + "#settings")])
    kb = InlineKeyboardMarkup(kb_rows)
    tzname = us.timezone or settings(ctx).default_tz
    await update.effective_message.reply_text(
        f"Settings\n\n"
        f"⏰ Time zone: <b>{tzname}</b>\n"
        f"🔔 Snooze buttons: <b>{'hidden' if us.hide_snooze else 'visible'}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Callback handlers (inline buttons)
# ---------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    us = user_state(ctx, update)

    # ---- onboarding / settings panels ----
    if data == "setup:chat":
        text, kb = _setup_chat_panel()
        await q.message.reply_text(text, reply_markup=kb)
        return

    if data.startswith("tz:"):
        name = data.split(":", 1)[1]
        if name == "other":
            await q.message.reply_text(
                "Send your IANA time zone (e.g. Europe/Amsterdam):"
            )
            ctx.user_data["awaiting_tz"] = True
            return
        us.timezone = name
        ctx.user_data["awaiting_tz"] = False
        await vault(ctx).save()
        await q.message.reply_text(
            f"✅ Time zone set to <b>{name}</b>.", parse_mode="HTML"
        )
        if not us.onboarded:
            await _finish_onboarding(update, ctx)
        return

    if data.startswith("set:tz"):
        text, kb = _setup_chat_panel()
        await q.message.reply_text(text, reply_markup=kb)
        return

    if data.startswith("set:snooze"):
        us.hide_snooze = not us.hide_snooze
        await vault(ctx).save()
        await q.edit_message_text(
            f"Snooze buttons: <b>{'hidden' if us.hide_snooze else 'visible'}</b> "
            "(applies to the mini app)",
            parse_mode="HTML",
        )
        return

    # ---- legacy menu callbacks (kept harmless) ----
    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        if action == "list":
            await cmd_list(update, ctx)
        elif action == "help":
            await cmd_help(update, ctx)
        elif action == "settings":
            await cmd_settings(update, ctx)
        elif action == "miniapp":
            await cmd_miniapp(update, ctx)
        elif action == "create":
            await q.message.reply_text(
                "Just send me a message like:\n"
                "  <code>every 10-20m stretch</code>\n"
                "  <code>every 90s water</code>\n"
                "  <code>every day 09:00 call mom</code>\n"
                "  <code>mon-fri 12:00 lunch</code>",
                parse_mode="HTML",
            )
        return


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-form text handler: secret phrase, timezone, or reminder creation."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    msg = update.effective_message
    text = (msg.text or "").strip()
    us = user_state(ctx, update)

    # 0. gate: unverified users must verify with the secret phrase
    if not us.verified:
        if text.lower() == settings(ctx).secret_phrase.lower():
            us.verified = True
            ctx.user_data["awaiting_phrase"] = False
            await vault(ctx).save()
            await _send_welcome(update, ctx)
        else:
            ctx.user_data["awaiting_phrase"] = True
            await verify_gate(ctx, update)
        return

    # 2. timezone setting (from the 'type another time zone' button)
    if ctx.user_data.get("awaiting_tz"):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(text)
        except Exception:  # noqa: BLE001
            await msg.reply_text("❌ Not a valid IANA timezone. Try e.g. Europe/Amsterdam.")
            return
        us.timezone = text
        ctx.user_data["awaiting_tz"] = False
        await vault(ctx).save()
        await msg.reply_text(f"✅ Time zone set to {text}.")
        if not us.onboarded:
            await _finish_onboarding(update, ctx)
        return

    # 3. natural-language-ish reminder creation
    tz_name = us.timezone or settings(ctx).default_tz
    parsed = _try_parse_create(text, tz_name)
    if parsed:
        gid = vault(ctx).db.next_id()
        created = []
        for r in parsed:
            r.id = vault(ctx).db.next_id()
            r.user_id = update.effective_user.id
            r.tz = tz_name
            r.group_id = gid
            await sched(ctx).add(r)
            created.append(r)
        first = created[0]
        n = len(created)
        await msg.reply_text(
            f"✅ Created #{first.id}" + (f" (+{n-1} schedule(s))" if n > 1 else "") +
            f": {_render_reminder_line(first, first.tz)}\n"
            "Manage it in the mini app."
        )
        return

    await msg.reply_text(
        "I didn't understand that. Try:\n"
        "  <code>every 10-20m stretch</code>\n"
        "  <code>every day 09:00 call mom</code>\n"
        "  <code>mon-fri 12:00 lunch</code>\n"
        "or /formats for all supported syntax.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Natural-language-ish parser for reminder creation
# ---------------------------------------------------------------------------

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$", re.IGNORECASE)
_RANGE_RE = re.compile(
    r"^\s*(\d+)\s*([smhd])?\s*-\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE
)
_RANGE_RE2 = re.compile(
    r"^(\d+)\s*([smhd]?)\s*-\s*(\d+)\s*([smhd])\s*(.*)$", re.IGNORECASE
)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):([0-5]\d)\s*$")
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_WEEKDAY_GROUPS = {"weekday": [0, 1, 2, 3, 4], "weekdays": [0, 1, 2, 3, 4], "weekend": [5, 6]}

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def _dur_to_seconds(n: int, unit: str) -> float:
    unit = unit.lower()
    return n * _UNIT_SECONDS.get(unit, 86400)


def _norm_time(s: str | None) -> str | None:
    if not s:
        return None
    m = _TIME_RE.match(s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _parse_dur(s: str) -> float | None:
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*([a-z]+)$", s)
    if not m:
        return None
    mult = _UNIT_SECONDS.get(m.group(2))
    return None if mult is None else int(m.group(1)) * mult


def _local_to_utc(dt: datetime, tz_name: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name or "UTC"))
    return dt.astimezone(timezone.utc).isoformat()


def _extract_at(rem: str, default_tod: str) -> tuple[str, str | None]:
    rem = rem.strip()
    m = re.match(r"^at\s+(\d{1,2}):([0-5]\d)\s*(.*)$", rem, re.I)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}", (m.group(3).strip() or None)
    parts = rem.split()
    if parts and _TIME_RE.match(parts[0]):
        h, minute = parts[0].split(":")
        return f"{int(h):02d}:{minute}", (" ".join(parts[1:]).strip() or None)
    return default_tod, (rem or None)


def _mk(text: str, tz: str, **kw) -> Reminder:
    return Reminder(id=0, user_id=0, text=text, kind="reminder", tz=tz, **kw)


def _parse_every(rest: str, tz: str) -> Reminder | None:
    low = rest.strip().lower()
    if not low:
        return None
    rm2 = _RANGE_RE2.match(low)
    if rm2:
        lo = _dur_to_seconds(int(rm2.group(1)), rm2.group(2) or rm2.group(4))
        hi = _dur_to_seconds(int(rm2.group(3)), rm2.group(4))
        note = rm2.group(5).strip() or f"every {rm2.group(1)}-{rm2.group(3)}{rm2.group(4)}"
        return _mk(note, tz, schedule_type="interval", min_seconds=lo, max_seconds=hi)
    wm = re.match(r"^([a-z]+(?:-[a-z]+)?)\s*(.*)$", low)
    if wm:
        first = wm.group(1)
        rem = wm.group(2).strip()
        weekdays = None
        if first in _WEEKDAY_GROUPS:
            weekdays = _WEEKDAY_GROUPS[first]
        elif first in _WEEKDAY_NAMES:
            weekdays = [_WEEKDAY_NAMES[first]]
        elif "-" in first:
            a, b = first.split("-")
            if a in _WEEKDAY_NAMES and b in _WEEKDAY_NAMES:
                lo_i, hi_i = _WEEKDAY_NAMES[a], _WEEKDAY_NAMES[b]
                if lo_i > hi_i:
                    lo_i, hi_i = hi_i, lo_i
                weekdays = list(range(lo_i, hi_i + 1))
        if weekdays is not None:
            tod, note = _extract_at(rem, "09:00")
            return _mk(note or f"every {first}", tz, schedule_type="weekdays",
                       time_of_day=tod, weekdays=weekdays)
        if first in ("day", "daily"):
            tod, note = _extract_at(rem, "09:00")
            return _mk(note or "every day", tz, schedule_type="every_day",
                       time_of_day=tod)
    dm = re.match(r"^(\d+)\s*([a-z]+)\s*(.*)$", low)
    if dm:
        n = int(dm.group(1))
        unit = dm.group(2).lower()
        rem = dm.group(3).strip()
        if unit in ("w", "week", "weeks"):
            period_days = n * 7
        elif unit in ("d", "day", "days"):
            period_days = n
        else:
            secs = n * _UNIT_SECONDS.get(unit, 60)
            note = rem or f"every {n}{unit}"
            return _mk(note, tz, schedule_type="interval",
                       min_seconds=secs, max_seconds=secs)
        return _every_ndays(n, unit, period_days, rem, tz)
    return None


def _every_ndays(n: int, unit: str, period_days: int, rem: str, tz: str) -> Reminder:
    label = f"every {n}{unit}"
    rm = re.match(r"^at\s+random\s*(.*)$", rem, re.I)
    if rm:
        rest = rm.group(1).strip()
        win = re.match(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s*(.*)$", rest)
        ws = we = None
        note = None
        if win:
            ws = _norm_time(win.group(1))
            we = _norm_time(win.group(2))
            note = win.group(3).strip() or None
            label = f"every {n}{unit} at random {ws}-{we}"
        else:
            note = rest or None
        return _mk(note or label, tz, schedule_type="every_ndays",
                   period_days=period_days, random_time=True,
                   window_start=ws, window_end=we)
    tm = re.match(r"^at\s+(\d{1,2}):([0-5]\d)\s*(.*)$", rem, re.I)
    if tm:
        tod = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        note = tm.group(3).strip() or None
        return _mk(note or f"every {n}{unit} at {tod}", tz,
                   schedule_type="every_ndays", period_days=period_days, time_of_day=tod)
    note = rem or f"every {n}{unit}"
    secs = period_days * 86400
    return _mk(note, tz, schedule_type="interval", min_seconds=secs, max_seconds=secs)


def _parse_once(s: str, tz: str) -> Reminder | None:
    m = re.match(r"^in\s+(.+)$", s, re.I)
    if m:
        after = m.group(1).strip()
        dm = re.match(r"^(\d+\s*[a-z]+)\s*(.*)$", after, re.I)
        dur = after if not dm else dm.group(1).strip()
        note = "" if not dm else dm.group(2).strip()
        secs = _parse_dur(dur)
        if secs is None:
            return None
        at = (utcnow() + timedelta(seconds=secs)).isoformat()
        return _mk(note or f"in {dur}", tz, schedule_type="once", at=at)
    m = re.match(r"^tomorrow\s+(?:at\s+)?(\d{1,2}):([0-5]\d)\s*(.*)$", s, re.I)
    if m:
        note = m.group(3).strip() or "tomorrow"
        now_local = utcnow().astimezone(ZoneInfo(tz or "UTC"))
        dt = (now_local + timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return _mk(note, tz, schedule_type="once", at=_local_to_utc(dt, tz))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):([0-5]\d)\s*(.*)$", s)
    if m:
        note = m.group(4).strip() or "scheduled"
        dt = datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]), int(m.group(1)[8:10]),
                      int(m.group(2)), int(m.group(3)))
        return _mk(note, tz, schedule_type="once", at=_local_to_utc(dt, tz))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*(.*)$", s)
    if m:
        note = m.group(2).strip() or "scheduled"
        return _mk(note, tz, schedule_type="once",
                   at=_local_to_utc(datetime.fromisoformat(m.group(1)), tz))
    return None


def _parse_one(spec: str, tz: str) -> Reminder | None:
    s = spec.strip()
    low = s.lower()
    m = re.match(r"^cron\s+(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s*(.*)$", s, re.I)
    if m:
        expr = " ".join(m.group(1).split())
        note = m.group(2).strip() or f"cron {expr}"
        if len(expr.split()) == 5:
            return _mk(note, tz, schedule_type="cron", cron_expr=expr)
    if low.startswith("every"):
        r = _parse_every(s[len("every"):].strip(), tz)
        if r is not None:
            return r
    r = _parse_once(s, tz)
    if r is not None:
        return r
    wm = re.match(r"^([a-z]+(?:-[a-z]+)?)\s+(?:at\s+)?(\d{1,2}):([0-5]\d)\s*(.*)$", low)
    if wm:
        first = wm.group(1)
        if first in _WEEKDAY_NAMES or "-" in first:
            if "-" in first:
                a, b = first.split("-")
                if a in _WEEKDAY_NAMES and b in _WEEKDAY_NAMES:
                    lo_i, hi_i = _WEEKDAY_NAMES[a], _WEEKDAY_NAMES[b]
                    if lo_i > hi_i:
                        lo_i, hi_i = hi_i, lo_i
                    weekdays = list(range(lo_i, hi_i + 1))
                else:
                    return None
            else:
                weekdays = [_WEEKDAY_NAMES[first]]
            tod = f"{int(wm.group(2)):02d}:{wm.group(3)}"
            note = wm.group(4).strip() or f"weekdays {first}"
            return _mk(note, tz, schedule_type="weekdays", time_of_day=tod, weekdays=weekdays)
    return None


def _try_parse_create(text: str, tz_name: str = "UTC") -> list[Reminder]:
    """Parse a free-form message into a list of Reminder specs."""
    parsed: list[Reminder] = []
    for part in re.split(r"\s*[·|]\s*", text):
        part = part.strip()
        if not part:
            continue
        r = _parse_one(part, tz_name)
        if r is not None:
            r.tz = tz_name
            parsed.append(r)
    return parsed
    return None



# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start", "start using the bot or return to the main menu"),
    BotCommand("list", "show your reminders"),
    BotCommand("delete", "delete a reminder (e.g. /delete 1)"),
    BotCommand("help", "open Help"),
    BotCommand("formats", "view supported date and time formats"),
    BotCommand("help_reminder_types", "the difference between reminders and tasks"),
    BotCommand("help_schedules", "what the icons next to a reminder's times mean"),
    BotCommand("cancel", "cancel the current action"),
    BotCommand("miniapp", "open the app to create and manage reminders"),
    BotCommand("settings", "open the settings"),
]


def register_handlers(application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("formats", cmd_formats))
    application.add_handler(CommandHandler("help_reminder_types", cmd_help_reminder_types))
    application.add_handler(CommandHandler("help_schedules", cmd_help_schedules))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("miniapp", cmd_miniapp))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
