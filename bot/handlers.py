"""Telegram chat handlers.

All commands go through the secret-phrase gate on first use. Once a user
is verified, they get the full menu.

Commands:
  /start               - start / return to main menu (asks secret phrase)
  /list                - show reminders
  /help                - open help
  /formats             - supported time formats
  /help_reminder_types - difference between reminder / task / task_no_notify
  /help_schedules      - what the schedule icons mean
  /cancel              - cancel the current inline action
  /miniapp             - open the Skeddy-style mini app
  /settings            - open settings (timezone, snooze, language)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
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

SNOOZE_OPTIONS = [
    ("+1m", 60),
    ("+5m", 300),
    ("+15m", 900),
    ("+1h", 3600),
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


def verify_gate(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    """Return True if the user is verified, else send the prompt and False."""
    if is_verified(ctx, update):
        return True
    settings = settings(ctx)
    msg = (
        "🔒 This bot is private.\n"
        "Type the secret phrase to continue:\n"
        f"<code>{settings.secret_phrase}</code>"
    )
    update.effective_message.reply_text(msg, parse_mode="HTML")
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
    }.get(r.schedule_type, "❓")


def _render_reminder_line(r: Reminder, user_tz: str) -> str:
    icon = _schedule_icon(r)
    kind_tag = {
        "reminder": "R",
        "task": "T",
        "task_no_notify": "S",
    }.get(r.kind, "?")
    parts = [f"{icon} #{r.id} [{kind_tag}] {r.text}"]
    if r.next_fire:
        parts.append(f"· next: {_fmt_next(r, user_tz)}")
    if r.note:
        parts.append(f"· note: {r.note}")
    return "\n".join(parts)


def _reminder_keyboard(r: Reminder, hide_snooze: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if r.kind == "task" and not hide_snooze:
        row = []
        for label, secs in SNOOZE_OPTIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"snooze:{r.id}:{secs}"))
        buttons.append(row)
    row = [InlineKeyboardButton("✅ Done", callback_data=f"done:{r.id}")]
    row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"del:{r.id}"))
    buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def _render_reminder_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE, r: Reminder) -> None:
    us = user_state(ctx, update)
    tzname = us.timezone or settings(ctx).default_tz
    text = _render_reminder_line(r, tzname)
    kb = _reminder_keyboard(r, hide_snooze=us.hide_snooze)
    await update.effective_message.reply_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — start using the bot or return to the main menu."""
    if not is_verified(ctx, update):
        # First contact: ask for the secret phrase.
        ctx.user_data["awaiting_phrase"] = True
        update.effective_message.reply_text(
            "🔒 This bot is private.\n"
            "Type the secret phrase to continue.\n"
            f"(Hint: it's in your onboarding notes.)"
        )
        return
    ctx.user_data["awaiting_phrase"] = False
    _main_menu(update, ctx)


def _main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 List", callback_data="menu:list"),
                InlineKeyboardButton("➕ Create", callback_data="menu:create"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
                InlineKeyboardButton("❓ Help", callback_data="menu:help"),
            ],
            [
                InlineKeyboardButton("🖥 Mini app", callback_data="menu:miniapp"),
            ],
        ]
    )
    update.effective_message.reply_text(
        "Skeddy — your reminder bot.\nPick an action:", reply_markup=keyboard
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — show your reminders."""
    if not verify_gate(ctx, update):
        return
    us = user_state(ctx, update)
    tzname = us.timezone or settings(ctx).default_tz
    reminders = [
        r for r in vault(ctx).db.reminders.values() if r.user_id == update.effective_user.id
    ]
    if not reminders:
        update.effective_message.reply_text(
            "No reminders yet. Create one via chat or the mini app."
        )
        return
    reminders.sort(key=lambda r: r.next_fire or "9999")
    lines = [f"📋 Your reminders ({len(reminders)}):", ""]
    for r in reminders:
        lines.append(_render_reminder_line(r, tzname))
        lines.append("")
    # one keyboard per reminder (Telegram limit: 100 buttons total)
    for r in reminders:
        kb = _reminder_keyboard(r, hide_snooze=us.hide_snooze)
        await update.effective_message.reply_text(
            _render_reminder_line(r, tzname), reply_markup=kb
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — open Help."""
    if not verify_gate(ctx, update):
        return
    text = (
        "Skeddy help\n\n"
        "Commands\n"
        "/start — main menu\n"
        "/list — your reminders\n"
        "/formats — supported time formats\n"
        "/help_reminder_types — reminder vs task\n"
        "/help_schedules — schedule icons\n"
        "/cancel — cancel the current action\n"
        "/miniapp — open the app\n"
        "/settings — timezone & options\n\n"
        "Creating reminders\n"
        "Use the mini app for the full editor, or reply to a reminder with"
        " 'done' to complete a task."
    )
    update.effective_message.reply_text(text)


async def cmd_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/formats — view supported date and time formats."""
    if not verify_gate(ctx, update):
        return
    text = (
        "Time formats\n\n"
        "Durations (for intervals):\n"
        "  45s  — 45 seconds\n"
        "  10m  — 10 minutes\n"
        "  2h   — 2 hours\n"
        "  3d   — 3 days\n\n"
        "Randomized intervals (the main feature):\n"
        "  10m-20m   — fire every 10 to 20 minutes, random\n"
        "  1h-2h     — fire every 1 to 2 hours, random\n"
        "  30s-2m    — mixed units are fine\n\n"
        "Clock times (for 'every day', 'weekdays', 'monthdays', 'date range'):\n"
        "  09:00     — 9am in your timezone\n"
        "  21:30     — 9:30pm\n\n"
        "Dates (for 'date range'):\n"
        "  2026-10-20 — ISO date\n\n"
        "Weekdays: Mon, Tue, Wed, Thu, Fri, Sat, Sun\n"
        "Days of month: 1..31"
    )
    update.effective_message.reply_text(text)


async def cmd_help_reminder_types(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help_reminder_types — the difference between reminders and tasks."""
    if not verify_gate(ctx, update):
        return
    text = (
        "Reminder types\n\n"
        "🔔 Reminder\n"
        "  I send it and complete it for you.\n\n"
        "📝 Task\n"
        "  I send it and it waits for you to complete it.\n"
        "  You get snooze buttons (+1m, +5m, +15m, +1h).\n\n"
        "🤫 Task with no notifications\n"
        "  I don't send it; it just waits for you to complete it."
        "  Useful for quiet tracking."
    )
    update.effective_message.reply_text(text)


async def cmd_help_schedules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help_schedules — what the icons next to a reminder's times mean."""
    if not verify_gate(ctx, update):
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
    update.effective_message.reply_text(text)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — cancel the current action."""
    if not verify_gate(ctx, update):
        return
    ctx.user_data["awaiting_phrase"] = False
    # also clear any in-progress callback state
    ctx.user_data.pop("pending_schedule", None)
    ctx.user_data.pop("pending_kind", None)
    ctx.user_data.pop("pending_text", None)
    update.effective_message.reply_text("Cancelled. Use /start to begin again.")


async def cmd_miniapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/miniapp — open the app."""
    if not verify_gate(ctx, update):
        return
    url = settings(ctx).bot_token  # placeholder; real URL set in config
    # The mini app URL is the same origin as the FastAPI service.
    # We read it from an env var MINI_APP_URL, or fall back to the
    # deployment URL.
    import os
    app_url = os.getenv("MINI_APP_URL", "").strip()
    if not app_url:
        update.effective_message.reply_text(
            "Mini app is not configured yet. Set the MINI_APP_URL environment "
            "variable to your Render deploy URL (e.g. https://randomreminders.onrender.com)."
        )
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖥 Open mini app", url=app_url)]]
    )
    update.effective_message.reply_text("Opening Skeddy mini app:", reply_markup=kb)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings — open the settings."""
    if not verify_gate(ctx, update):
        return
    us = user_state(ctx, update)
    import os
    app_url = os.getenv("MINI_APP_URL", "").strip()
    kb_rows = []
    if app_url:
        kb_rows.append([InlineKeyboardButton("⚙️ Open settings in mini app", url=app_url + "#settings")])
    kb_rows.append(
        [
            InlineKeyboardButton("🌐 Timezone", callback_data="set:tz"),
            InlineKeyboardButton("🔕 Snooze", callback_data="set:snooze"),
        ]
    )
    kb = InlineKeyboardMarkup(kb_rows)
    update.effective_message.reply_text(
        f"Current timezone: <b>{us.timezone or settings(ctx).default_tz}</b>\n"
        f"Snooze buttons: <b>{'hidden' if us.hide_snooze else 'visible'}</b>",
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
            q.message.reply_text(
                "Use the mini app to create a reminder, or send me a message like:\n"
                "  <code>every 10-20m stretch</code>\n"
                "  <code>every 90s water</code>\n"
                "  <code>every day 09:00 call mom</code>\n"
                "  <code>mon-fri 12:00 lunch</code>",
                parse_mode="HTML",
            )
        return

    if data.startswith("snooze:"):
        _, rid, secs = data.split(":")
        rid_i, secs_i = int(rid), int(secs)
        r = vault(ctx).db.reminders.get(rid_i)
        if r is None:
            return
        # push the next fire forward by secs
        now = utcnow()
        nxt = now + timedelta(seconds=secs_i)
        r.next_fire = nxt.isoformat()
        sched(ctx)._reschedule_one(r, now)
        await vault(ctx).save()
        await q.edit_message_text(
            f"Snoozed #{rid_i} by {_fmt_duration(secs_i)}. New fire: {_fmt_next(r, user_state(ctx, update).timezone or settings(ctx).default_tz)}"
        )
        return

    if data.startswith("done:"):
        rid_i = int(data.split(":", 1)[1])
        r = vault(ctx).db.reminders.get(rid_i)
        if r is None:
            return
        r.completed = True
        if r.kind == "task":
            await sched(ctx).remove(rid_i)
            await q.edit_message_text(f"✅ Task #{rid_i} completed.")
        else:
            await q.edit_message_text(f"✅ Done with #{rid_i}.")
        return

    if data.startswith("del:"):
        rid_i = int(data.split(":", 1)[1])
        await sched(ctx).remove(rid_i)
        await q.edit_message_text(f"🗑 Deleted reminder #{rid_i}.")
        return

    if data.startswith("set:tz"):
        # ask for timezone inline
        await q.message.reply_text("Send your IANA timezone (e.g. Europe/Amsterdam):")
        ctx.user_data["awaiting_tz"] = True
        return

    if data.startswith("set:snooze"):
        us = user_state(ctx, update)
        us.hide_snooze = not us.hide_snooze
        await vault(ctx).save()
        await q.edit_message_text(
            f"Snooze buttons: <b>{'hidden' if us.hide_snooze else 'visible'}</b>",
            parse_mode="HTML",
        )
        return


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-form text handler: secret phrase, timezone, or reminder creation."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    us = user_state(ctx, update)

    # 1. secret phrase verification
    if ctx.user_data.get("awaiting_phrase"):
        if text.lower() == settings(ctx).secret_phrase.lower():
            us.verified = True
            ctx.user_data["awaiting_phrase"] = False
            await vault(ctx).save()
            await msg.reply_text("✅ Verified. Welcome!")
            _main_menu(update, ctx)
        else:
            await msg.reply_text("❌ Wrong phrase. Try again, or /start to reset.")
        return

    # 2. timezone setting
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
        await msg.reply_text(f"✅ Timezone set to {text}.")
        return

    # 3. natural-language-ish reminder creation
    parsed = _try_parse_create(text)
    if parsed is not None:
        r = parsed
        r.id = vault(ctx).db.next_id()
        r.user_id = update.effective_user.id
        r.tz = us.timezone or settings(ctx).default_tz
        await sched(ctx).add(r)
        await msg.reply_text(
            f"✅ Created #{r.id}: {_render_reminder_line(r, r.tz)}",
            reply_markup=_reminder_keyboard(r, hide_snooze=us.hide_snooze),
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

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
# Two forms: "10m-20m" (independent units) and "10-20m" (shared unit)
_RANGE_RE = re.compile(
    r"^\s*(\d+)\s*([smhd])?\s*-\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE
)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):([0-5]\d)\s*$")
_WEEKDAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _dur_to_seconds(n: int, unit: str) -> float:
    unit = unit.lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _try_parse_create(text: str) -> Reminder | None:
    """Parse a free-form message into a Reminder (without id/user_id)."""
    tokens = text.split()
    if not tokens:
        return None
    head = tokens[0].lower()

    if head == "every":
        if len(tokens) < 2:
            return None
        second = tokens[1]
        m = _RANGE_RE.match(second)
        if m:
            lo = _dur_to_seconds(int(m.group(1)), m.group(2) or m.group(4))
            hi = _dur_to_seconds(int(m.group(3)), m.group(4))
            note = " ".join(tokens[2:]) or None
            return Reminder(
                id=0, user_id=0,
                text=note or f"every {second}",
                kind="reminder", schedule_type="interval",
                min_seconds=lo, max_seconds=hi, tz="UTC",
            )
        m = _DURATION_RE.match(second)
        if m:
            secs = _dur_to_seconds(int(m.group(1)), m.group(2))
            note = " ".join(tokens[2:]) or None
            return Reminder(
                id=0, user_id=0, text=note or f"every {second}",
                kind="reminder", schedule_type="interval",
                min_seconds=secs, max_seconds=secs, tz="UTC",
            )
        if second in ("day", "daily"):
            tod, note = "09:00", None
            if len(tokens) > 2:
                m = _TIME_RE.match(tokens[2])
                if m:
                    tod = f"{m.group(1).zfill(2)}:{m.group(2)}"
                    note = " ".join(tokens[3:]) or None
            return Reminder(
                id=0, user_id=0, text=note or "every day",
                kind="reminder", schedule_type="every_day",
                time_of_day=tod, tz="UTC",
            )
        return None

    if head in _WEEKDAY_NAMES or re.match(r"^[a-z]+-[a-z]+$", head):
        m = re.match(r"^([a-z]+)-([a-z]+)$", head)
        if m:
            a, b = m.group(1).lower(), m.group(2).lower()
            if a not in _WEEKDAY_NAMES or b not in _WEEKDAY_NAMES:
                return None
            lo_i, hi_i = _WEEKDAY_NAMES[a], _WEEKDAY_NAMES[b]
            if lo_i > hi_i:
                lo_i, hi_i = hi_i, lo_i
            weekdays = list(range(lo_i, hi_i + 1))
        else:
            weekdays = [_WEEKDAY_NAMES[head]]
        tod, note = "09:00", None
        if len(tokens) > 1:
            m = _TIME_RE.match(tokens[1])
            if m:
                tod = f"{m.group(1).zfill(2)}:{m.group(2)}"
                note = " ".join(tokens[2:]) or None
        return Reminder(
            id=0, user_id=0, text=note or f"weekdays {head}",
            kind="reminder", schedule_type="weekdays",
            time_of_day=tod, weekdays=weekdays, tz="UTC",
        )

    return None



# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start", "start using the bot or return to the main menu"),
    BotCommand("list", "show your reminders"),
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
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("formats", cmd_formats))
    application.add_handler(CommandHandler("help_reminder_types", cmd_help_reminder_types))
    application.add_handler(CommandHandler("help_schedules", cmd_help_schedules))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("miniapp", cmd_miniapp))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
