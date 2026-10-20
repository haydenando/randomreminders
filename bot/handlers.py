"""Telegram chat handlers.

All commands go through the secret-phrase gate on first use. Once verified,
the user gets a Gently Reminding onboarding (mini app button or chat setup with
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
  /miniapp             - open the Gently Reminding mini app
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
    """First onboarding screen: mini app button + chat setup (Gently Reminding)."""
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
    """Welcome screen for a verified user (Gently Reminding)."""
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
        "Gently Reminding Bot help\n\n"
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
    await update.effective_message.reply_text("Opening Gently Reminding Bot mini app:", reply_markup=kb)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings — open the settings (Gently Reminding panel)."""
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
    parsed = _try_parse_create(text)
    if parsed is not None:
        r = parsed
        r.id = vault(ctx).db.next_id()
        r.user_id = update.effective_user.id
        r.tz = us.timezone or settings(ctx).default_tz
        await sched(ctx).add(r)
        await msg.reply_text(
            f"✅ Created #{r.id}: {_render_reminder_line(r, r.tz)}\n"
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
