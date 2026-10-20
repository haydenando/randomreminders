"""Shared configuration and small helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    vault_token: str
    vault_chat_id: int
    secret_phrase: str
    default_tz: str


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy .env.example to .env and fill it in, or set it in Render."
        )
    return value


def load_settings() -> Settings:
    return Settings(
        bot_token=_require("BOT_TOKEN"),
        vault_token=_require("VAULT_TOKEN"),
        vault_chat_id=int(_require("VAULT_CHAT_ID")),
        secret_phrase=os.getenv("SECRET_PHRASE", "haydenando").strip(),
        default_tz=os.getenv("DEFAULT_TIMEZONE", "UTC").strip() or "UTC",
    )


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(load_settings().default_tz)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Reminder model
# ---------------------------------------------------------------------------

# Schedule kinds:
#   "once"         - fire at a specific datetime, then stop
#   "every_day"    - fire at HH:MM in the user's timezone, every day
#   "interval"     - fire every N seconds (or uniformly random in [min, max])
#   "weekdays"     - fire at HH:MM on selected weekdays (Mon=0 .. Sun=6)
#   "monthdays"    - fire at HH:MM on selected day-of-month numbers
#   "daterange"    - fire at HH:MM every day while today is in [start, end]

# Reminder kinds:
#   "reminder"   - regular reminder (send and auto-complete)
#   "countdown"  - countdown to a target date/time (may pre-remind N days before)


@dataclass
class Reminder:
    id: int
    user_id: int
    text: str
    kind: str                      # reminder | countdown
    schedule_type: str             # once | every_day | interval | weekdays | monthdays | daterange
    # schedule params (only the relevant ones are set)
    at: str | None = None          # ISO datetime (once)
    time_of_day: str | None = None # "HH:MM"
    min_seconds: float | None = None
    max_seconds: float | None = None
    weekdays: list[int] = field(default_factory=list)        # 0=Mon
    monthdays: list[int] = field(default_factory=list)       # 1..31
    range_start: str | None = None   # "YYYY-MM-DD"
    range_end: str | None = None     # "YYYY-MM-DD"
    tz: str = "UTC"
    next_fire: str | None = None     # ISO datetime of the next fire (UTC)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    last_fire: str | None = None
    completed: bool = False
    # optional image to send with the reminder (uploaded image, Vault-stored)
    image_id: str | None = None
    # legacy field kept so old vault data still loads (Telegram photo file_id)
    image_file_id: str | None = None
    # optional image to send with the reminder (Telegram photo file_id)
    image_file_id: str | None = None
    # customizable message template. Placeholders:
    #   {text}  - the reminder text
    #   {id}    - reminder id
    #   {next}  - humanized next-fire time
    message_template: str | None = None
    # per-reminder note (free text, shown in /list and mini app)
    note: str | None = None
    # free-form tags (UI adornment)
    tags: list[str] = field(default_factory=list)
    # countdown source params (kept so edits are lossless)
    target_date: str | None = None        # "YYYY-MM-DD"
    notification_time: str | None = None  # "HH:MM"
    countdown_days_before: int | None = None
    # every_ndays schedule params (e.g. "every 4d at 18:00")
    period_days: int | None = None        # period in days
    random_time: bool = False             # "at random" -> pick a time within window
    window_start: str | None = None       # "HH:MM" (random window start)
    window_end: str | None = None         # "HH:MM" (random window end)
    # cron schedule param (5-field crontab string)
    cron_expr: str | None = None
    # group schedules that belong to the same logical reminder
    group_id: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Reminder":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
