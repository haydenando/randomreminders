"""Persistence layer.

All state lives in a single hidden Telegram message (the "vault message")
owned by the user's vault bot, in a private chat with the user.

Why a Telegram message instead of a file?
- Render free-tier dynos wipe the filesystem on every redeploy.
- A Telegram message lives forever in Telegram's backend.
- No database, no persistent disk, nothing to maintain.

The vault message looks like:
    <<<RRv1>>>
    {"schema":1,"reminders":[...],"users":{...},"seq":12}
    <<<RRv1>>>

We pin the vault message in the vault chat so future boots can find it
via get_chat().pinned_message.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from telegram import Bot

from .config import Reminder, Settings

log = logging.getLogger(__name__)

MARKER = "<<<RRv1>>>"
SCHEMA_VERSION = 1


@dataclass
class UserState:
    verified: bool = False
    timezone: str | None = None
    hide_snooze: bool = False
    language: str | None = None


@dataclass
class DB:
    reminders: dict[int, Reminder] = field(default_factory=dict)
    users: dict[int, UserState] = field(default_factory=dict)
    seq: int = 0

    def user(self, user_id: int) -> UserState:
        if user_id not in self.users:
            self.users[user_id] = UserState()
        return self.users[user_id]

    def next_id(self) -> int:
        self.seq += 1
        return self.seq

    def to_payload(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "seq": self.seq,
                "users": {str(k): v.__dict__ for k, v in self.users.items()},
                "reminders": [r.to_dict() for r in self.reminders.values()],
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_payload(cls, raw: str) -> "DB":
        data = json.loads(raw)
        db = cls()
        db.seq = int(data.get("seq", 0))
        for uid, udata in (data.get("users") or {}).items():
            db.users[int(uid)] = UserState(
                **{
                    k: v
                    for k, v in udata.items()
                    if k in UserState.__dataclass_fields__
                }
            )
        for rdata in data.get("reminders") or []:
            r = Reminder.from_dict(rdata)
            db.reminders[r.id] = r
        return db


def wrap(payload: str) -> str:
    return f"{MARKER}\n{payload}\n{MARKER}"


def unwrap(text: str) -> str:
    if not text:
        raise ValueError("empty vault message")
    lines = text.strip().split("\n")
    if len(lines) < 3 or lines[0].strip() != MARKER or lines[-1].strip() != MARKER:
        raise ValueError("vault message is not in RRv1 format")
    return "\n".join(lines[1:-1])


class Vault:
    """Reads and writes the vault message."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._vault_bot = Bot(token=settings.vault_token)
        self._lock = asyncio.Lock()
        self.db = DB()
        self._message_id: int | None = None

    async def setup(self) -> None:
        """Create or load the vault message. Idempotent, safe on every boot."""
        async with self._lock:
            try:
                chat = await self._vault_bot.get_chat(self.settings.vault_chat_id)
                pinned = getattr(chat, "pinned_message", None)
                if pinned is not None and MARKER in (
                    pinned.text or pinned.caption or ""
                ):
                    self.db = DB.from_payload(unwrap(pinned.text or pinned.caption or ""))
                    self._message_id = pinned.message_id
                    log.info("Loaded vault from pinned message %s", self._message_id)
                    return

                msg = await self._vault_bot.send_message(
                    self.settings.vault_chat_id, wrap(self.db.to_payload())
                )
                self._message_id = msg.message_id
                try:
                    await self._vault_bot.pin_chat_message(
                        self.settings.vault_chat_id, msg.message_id
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not pin vault message: %s", exc)
                log.info("Created new vault message %s", self._message_id)
            except Exception:
                log.exception("Vault setup failed")
                raise

    async def save(self) -> None:
        """Persist current DB state to the vault message."""
        async with self._lock:
            if self._message_id is None:
                await self.setup()
            payload = wrap(self.db.to_payload())
            try:
                await self._vault_bot.edit_message_text(
                    chat_id=self.settings.vault_chat_id,
                    message_id=self._message_id,
                    text=payload,
                )
            except Exception:
                log.warning("Edit failed, recreating vault message")
                msg = await self._vault_bot.send_message(
                    self.settings.vault_chat_id, payload
                )
                self._message_id = msg.message_id
                try:
                    await self._vault_bot.pin_chat_message(
                        self.settings.vault_chat_id, self._message_id
                    )
                except Exception:  # noqa: BLE001
                    pass

    def db_ref(self) -> DB:
        return self.db
