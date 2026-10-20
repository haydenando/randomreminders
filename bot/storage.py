"""Persistence layer.

All state lives in a private chat with the user's vault bot. The *index*
message is pinned, so it is always readable via get_chat().pinned_message.
The actual DB payload is stored either:
  * inline in the pinned index message (small databases), or
  * as a Telegram *document* (called vault.json) in the same chat, referenced
    by the file_id stored in the index.

This avoids Telegram's 4096-character per-message limit, which would otherwise
cap the database at a few dozen reminders. Documents allow up to 20 MB, so the
DB can grow to thousands of reminders/users before hitting any wall.

Index message (pinned), wrapped in <<<RRv2>>>:
    {"schema":2,"seq":12,"file":null,"d":"{...}"}
or, when stored as a document:
    {"schema":2,"seq":12,"file":"BQAC...","msgid":1234,"d":null}

Legacy v1 (<<<RRv1>>>) single-message payloads are migrated transparently on
the first save.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from telegram import Bot

from .config import Reminder, Settings

log = logging.getLogger(__name__)

# Legacy single-message vault marker (v1).
MARKER = "<<<RRv1>>>"
# v2 index message marker.
V2_MARKER = "<<<RRv2>>>"
SCHEMA_VERSION = 1
SCHEMA_VERSION_V2 = 2

# If the serialized DB payload is at or below this many characters we keep it
# inline in the pinned message (cheap, no upload). Above this we store it as a
# document. Kept well under Telegram's 4096-char message limit.
MAX_DIRECT_PAYLOAD = 3500

# Name of the document that holds the DB payload when it is too big for inline.
DB_FILENAME = "vault.json"

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image (kept for legacy)
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB per uploaded file (Bot API document limit)


@dataclass
class UserState:
    verified: bool = False
    timezone: str | None = None
    hide_snooze: bool = False
    onboarded: bool = False
    # Snooze durations in seconds (default 5m, 10m, 30m)
    snooze_durations: list[int] = field(default_factory=lambda: [300, 600, 1800])


@dataclass
class DB:
    reminders: dict[int, Reminder] = field(default_factory=dict)
    users: dict[int, UserState] = field(default_factory=dict)
    seq: int = 0
    # image_id -> vault-bot file_id of the image backup document
    image_refs: dict[str, str] = field(default_factory=dict)
    # image_id -> {"filename": str, "media_type": "photo" | "document"}
    image_meta: dict[str, dict] = field(default_factory=dict)

    def user(self, user_id: int) -> UserState:
        if user_id not in self.users:
            self.users[user_id] = UserState()
        return self.users[user_id]

    def next_id(self) -> int:
        self.seq += 1
        return self.seq

    def next_image_id(self) -> str:
        self.seq += 1
        return f"img{self.seq}"

    def to_payload(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA_VERSION_V2,
                "seq": self.seq,
                "users": {str(k): v.__dict__ for k, v in self.users.items()},
                "reminders": [r.to_dict() for r in self.reminders.values()],
                "image_refs": self.image_refs,
                "image_meta": self.image_meta,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_payload(cls, raw: str) -> "DB":
        data = json.loads(raw)
        db = cls()
        db.seq = int(data.get("seq", 0))
        db.image_refs = dict(data.get("image_refs") or {})
        db.image_meta = dict(data.get("image_meta") or {})
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


def wrap_v2(payload: str) -> str:
    return f"{V2_MARKER}\n{payload}\n{V2_MARKER}"


def unwrap_v2(text: str) -> str:
    if not text:
        raise ValueError("empty vault index")
    lines = text.strip().split("\n")
    if len(lines) < 3 or lines[0].strip() != V2_MARKER or lines[-1].strip() != V2_MARKER:
        raise ValueError("vault index is not in RRv2 format")
    return "\n".join(lines[1:-1])


class Vault:
    """Reads and writes the vault index + (optionally) the DB payload document."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._vault_bot = Bot(token=settings.vault_token)
        self._lock = asyncio.Lock()
        self.db = DB()
        # id of the pinned index message
        self._message_id: int | None = None
        # current DB payload document (when too big for inline)
        self._data_file_id: str | None = None
        self._data_message_id: int | None = None
        # last payload we successfully persisted; used to skip unchanged saves
        self._payload_cache: str | None = None
        # in-memory image cache: image_id -> bytes
        self._images: dict[str, bytes] = {}

    async def setup(self) -> None:
        """Create or load the vault. Idempotent, safe on every boot."""
        async with self._lock:
            try:
                chat = await self._vault_bot.get_chat(self.settings.vault_chat_id)
                pinned = getattr(chat, "pinned_message", None)
                raw = (pinned.text or pinned.caption or "") if pinned else ""

                if raw.strip().startswith(V2_MARKER):
                    await self._load_index(raw, pinned.message_id)
                    log.info("Loaded vault (v2) index %s", self._message_id)
                    return

                if MARKER in raw:
                    # Legacy v1 vault: load it, migrate on the next save.
                    self.db = DB.from_payload(unwrap(raw))
                    self._message_id = pinned.message_id
                    self._payload_cache = None
                    self._data_file_id = None
                    self._data_message_id = None
                    log.info("Loaded legacy vault (v1) %s; will migrate on next save", self._message_id)
                    return

                await self._send_fresh_index()
                log.info("Created new vault index %s", self._message_id)
            except Exception:
                log.exception("Vault setup failed")
                raise

    async def _load_index(self, raw: str, mid: int) -> None:
        try:
            data = json.loads(unwrap_v2(raw))
        except Exception:
            log.exception("Malformed v2 index; starting fresh")
            await self._send_fresh_index()
            return

        self._message_id = mid
        self.db = DB()
        self.db.seq = int(data.get("seq", 0))
        d = data.get("d")
        file_id = data.get("file")

        if d:  # inline payload
            try:
                self.db = DB.from_payload(d)
            except Exception:
                log.exception("Bad inline payload; starting fresh")
                self.db = DB()
            self._payload_cache = d
            self._data_file_id = None
            self._data_message_id = None
        elif file_id:  # payload stored as a document
            try:
                raw_payload = await self._download_payload(file_id)
                self.db = DB.from_payload(raw_payload)
                self._payload_cache = raw_payload
            except Exception:
                log.exception("Failed to load DB document; starting fresh")
                self.db = DB()
                self._payload_cache = None
            self._data_file_id = file_id
            self._data_message_id = data.get("msgid")
        else:  # empty vault
            self._payload_cache = self.db.to_payload()
            self._data_file_id = None
            self._data_message_id = None

    async def _download_payload(self, file_id: str) -> str:
        f = await self._vault_bot.get_file(file_id)
        data = await f.download_as_bytearray()
        return bytes(data).decode("utf-8")

    async def _send_fresh_index(self) -> None:
        body = wrap_v2(
            json.dumps(
                {"schema": SCHEMA_VERSION_V2, "seq": self.db.seq, "file": None, "d": self.db.to_payload()},
                separators=(",", ":"),
            )
        )
        msg = await self._vault_bot.send_message(self.settings.vault_chat_id, body)
        self._message_id = msg.message_id
        self._payload_cache = self.db.to_payload()
        self._data_file_id = None
        self._data_message_id = None
        await self._pin(msg.message_id)

    async def save(self) -> None:
        """Persist current DB state to the vault (inline or as a document)."""
        if self._message_id is None:
            await self.setup()
        async with self._lock:
            await self._persist()

    async def _persist(self) -> None:
        payload = self.db.to_payload()
        if payload == self._payload_cache:
            return

        if len(payload) <= MAX_DIRECT_PAYLOAD:
            # Inline: cheap, no upload.
            body = wrap_v2(
                json.dumps(
                    {"schema": SCHEMA_VERSION_V2, "seq": self.db.seq, "file": None, "d": payload},
                    separators=(",", ":"),
                )
            )
            ok = await self._write_index(body)
            if ok:
                self._payload_cache = payload
                if self._data_file_id:
                    await self._delete_data_message(self._data_message_id)
                self._data_file_id = None
                self._data_message_id = None
            return

        # Payload too big for a message -> store as a document.
        file_id, msg_id = await self._upload_data_file(payload)
        if not file_id:
            log.error("Failed to upload DB payload document (len=%d); keeping old", len(payload))
            return
        body = wrap_v2(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION_V2,
                    "seq": self.db.seq,
                    "file": file_id,
                    "msgid": msg_id,
                    "d": None,
                },
                separators=(",", ":"),
            )
        )
        ok = await self._write_index(body)
        if ok:
            # Drop the previous data document (not the one we just uploaded).
            if self._data_file_id:
                await self._delete_data_message(self._data_message_id)
            self._data_file_id = file_id
            self._data_message_id = msg_id
            self._payload_cache = payload

    async def _write_index(self, body: str) -> bool:
        try:
            await self._vault_bot.edit_message_text(
                chat_id=self.settings.vault_chat_id,
                message_id=self._message_id,
                text=body,
            )
            return True
        except Exception:
            log.warning("Index edit failed, recreating index message")
            try:
                msg = await self._vault_bot.send_message(self.settings.vault_chat_id, body)
                self._message_id = msg.message_id
                await self._pin(msg.message_id)
                return True
            except Exception:
                log.exception("Failed to recreate index message")
                return False

    async def _upload_data_file(self, payload: str) -> tuple[str | None, int | None]:
        try:
            msg = await self._vault_bot.send_document(
                self.settings.vault_chat_id,
                document=payload.encode("utf-8"),
                filename=DB_FILENAME,
            )
            return msg.document.file_id, msg.message_id
        except Exception:
            log.exception("Failed to upload DB payload document")
            return None, None

    async def _delete_data_message(self, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            await self._vault_bot.delete_message(self.settings.vault_chat_id, message_id)
        except Exception:
            log.debug("Could not delete old data message %s", message_id)

    async def _pin(self, message_id: int) -> None:
        try:
            await self._vault_bot.pin_chat_message(self.settings.vault_chat_id, message_id)
        except Exception:
            log.warning("Could not pin vault message %s", message_id)

    def db_ref(self) -> DB:
        return self.db

    # ---- image storage -------------------------------------------------------
    # Images are cached in memory as raw bytes and backed up in the vault chat
    # by the vault bot (as a *document*, so quality is preserved). The vault
    # bot's file_id for that document is stored in the vault JSON, so images
    # survive restarts; on a cache miss we re-download via the vault bot.

    async def put_image(self, data: bytes, filename: str = "file", media_type: str = "photo") -> str:
        """Store file bytes; return the new image_id."""
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file too large (max {MAX_FILE_BYTES // (1024*1024)} MB)")
        image_id = self.db.next_image_id()
        msg = await self._vault_bot.send_document(
            self.settings.vault_chat_id,
            document=data,
            filename=f"{image_id}_{filename}",
        )
        self.db.image_refs[image_id] = msg.document.file_id
        self.db.image_meta[image_id] = {"filename": filename, "media_type": media_type}
        self._images[image_id] = data
        await self.save()
        return image_id

    async def get_image(self, image_id: str | None) -> bytes | None:
        """Return cached bytes, or re-download from the vault-bot backup."""
        if not image_id:
            return None
        if image_id in self._images:
            return self._images[image_id]
        file_id = self.db.image_refs.get(image_id)
        if not file_id:
            return None
        try:
            f = await self._vault_bot.get_file(file_id)
            data = await f.download_as_bytearray()
            self._images[image_id] = bytes(data)
            return self._images[image_id]
        except Exception:  # noqa: BLE001
            log.exception("Failed to load image %s from vault backup", image_id)
            return None

    def has_image(self, image_id: str | None) -> bool:
        if not image_id:
            return False
        return image_id in self._images or image_id in self.db.image_refs

    def get_image_meta(self, image_id: str | None) -> dict:
        if not image_id:
            return {}
        return self.db.image_meta.get(image_id) or {}

    async def delete_image(self, image_id: str | None) -> None:
        """Drop an image if no other reminder still references it."""
        if not image_id:
            return
        still_used = any(r.image_id == image_id for r in self.db.reminders.values())
        if still_used:
            return
        self._images.pop(image_id, None)
        self.db.image_meta.pop(image_id, None)
        if self.db.image_refs.pop(image_id, None) is not None:
            await self.save()
