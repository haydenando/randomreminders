"""Tests for the document-backed vault storage.

Run with:  python -m tests.test_storage
No external Telegram connection is needed -- the vault bot is faked in memory.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.config import Reminder, Settings
from bot.storage import DB, UserState, Vault, wrap, unwrap_v2, V2_MARKER


def make_settings() -> Settings:
    return Settings(
        bot_token="bot-token",
        vault_token="vault-token",
        vault_chat_id=123,
        secret_phrase="secret",
        default_tz="UTC",
    )


def make_reminder(rid, text="hi", **kw) -> Reminder:
    base = dict(
        id=rid,
        user_id=1,
        text=text,
        kind="reminder",
        schedule_type="interval",
        min_seconds=60,
        max_seconds=120,
    )
    base.update(kw)
    return Reminder(**base)


# ---------------------------------------------------------------------------
# In-memory fake of the vault bot's Telegram API surface
# ---------------------------------------------------------------------------

class FakeDocument:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, message_id, text=None, document=None, caption=None):
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.document = document


class FakeChat:
    def __init__(self, pinned=None):
        self.pinned_message = pinned


class FakeFile:
    def __init__(self, data):
        self._data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class FakeVaultBot:
    def __init__(self):
        # message_id -> (text, doc_bytes)
        self.messages: dict[int, tuple[str | None, bytes | None]] = {}
        self.documents: dict[str, bytes] = {}
        self.pinned_id: int | None = None
        self._next_id = 100

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def get_chat(self, chat_id):
        if self.pinned_id is not None:
            text, _ = self.messages[self.pinned_id]
            return FakeChat(pinned=FakeMessage(self.pinned_id, text=text))
        return FakeChat(pinned=None)

    async def send_message(self, chat_id, text, **kw):
        mid = self._new_id()
        self.messages[mid] = (text, None)
        return FakeMessage(mid, text=text)

    async def edit_message_text(self, chat_id, message_id, text):
        if message_id not in self.messages:
            raise Exception("no such message")
        self.messages[message_id] = (text, self.messages[message_id][1])
        return FakeMessage(message_id, text=text)

    async def pin_chat_message(self, chat_id, message_id):
        self.pinned_id = message_id

    async def send_document(self, chat_id, document, filename=""):
        mid = self._new_id()
        file_id = f"file_{mid}_{filename}"
        self.documents[file_id] = document
        self.messages[mid] = (None, document)
        return FakeMessage(mid, document=FakeDocument(file_id))

    async def get_file(self, file_id):
        return FakeFile(self.documents[file_id])

    async def delete_message(self, chat_id, message_id):
        if message_id in self.messages:
            del self.messages[message_id]


def build_vault(fake) -> Vault:
    v = Vault(make_settings())
    v._vault_bot = fake
    return v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_db_roundtrip():
    db = DB()
    db.seq = 5
    db.users[1] = UserState(verified=True, timezone="UTC")
    for i in range(3):
        db.reminders[i] = make_reminder(i + 1, text=f"r{i}")
    db.image_refs["img1"] = "BQAC1"
    db.image_meta["img1"] = {"filename": "a.png", "media_type": "photo"}
    db2 = DB.from_payload(db.to_payload())
    assert db2.seq == 5
    assert len(db2.reminders) == 3
    assert db2.reminders[1].text == "r0"
    assert db2.users[1].verified is True
    assert db2.image_refs["img1"] == "BQAC1"


def test_index_roundtrip_wrap():
    body = json.dumps({"schema": 2, "seq": 3, "file": None, "d": "{...}"})
    wrapped = f"{V2_MARKER}\n{body}\n{V2_MARKER}"
    assert json.loads(unwrap_v2(wrapped))["seq"] == 3


async def test_fresh_setup_and_reload():
    fake = FakeVaultBot()
    v = build_vault(fake)
    await v.setup()
    assert v._message_id is not None
    assert fake.pinned_id == v._message_id
    v2 = build_vault(fake)
    await v2.setup()
    assert v2.db.seq == 0
    assert len(v2.db.reminders) == 0


async def test_save_small_inline():
    fake = FakeVaultBot()
    v = build_vault(fake)
    await v.setup()
    v.db.reminders[1] = make_reminder(1, "small")
    v.db.seq = 1
    await v.save()
    assert v._data_file_id is None
    v2 = build_vault(fake)
    await v2.setup()
    assert 1 in v2.db.reminders
    assert v2.db.reminders[1].text == "small"


async def test_save_large_becomes_document():
    fake = FakeVaultBot()
    v = build_vault(fake)
    await v.setup()
    for i in range(60):
        v.db.reminders[i] = make_reminder(i + 1, text="x" * 200)
    v.db.seq = 60
    await v.save()
    assert v._data_file_id is not None
    idx_text, _ = fake.messages[v._message_id]
    assert idx_text.lstrip().startswith(V2_MARKER)
    assert '"file"' in idx_text
    assert '"d":null' in idx_text
    v2 = build_vault(fake)
    await v2.setup()
    assert len(v2.db.reminders) == 60
    assert v2.db.reminders[30].text == "x" * 200
    assert v2.db.seq == 60


async def test_shrink_cleans_up_document():
    fake = FakeVaultBot()
    v = build_vault(fake)
    await v.setup()
    for i in range(60):
        v.db.reminders[i] = make_reminder(i + 1, text="x" * 200)
    v.db.seq = 60
    await v.save()
    assert v._data_file_id is not None
    old_doc_msg_id = v._data_message_id
    v.db.reminders.clear()
    v.db.reminders[1] = make_reminder(1, "small")
    v.db.seq = 61
    await v.save()
    assert v._data_file_id is None
    assert old_doc_msg_id not in fake.messages


async def test_migrate_v1():
    fake = FakeVaultBot()
    db = DB()
    db.seq = 7
    db.reminders[1] = make_reminder(1, "legacy", schedule_type="once", at="2026-01-01T00:00:00+00:00")
    db.users[1] = UserState(verified=True, timezone="UTC")
    payload = wrap(db.to_payload())
    mid = fake._new_id()
    fake.messages[mid] = (payload, None)
    fake.pinned_id = mid

    v = build_vault(fake)
    await v.setup()
    assert v.db.seq == 7
    assert 1 in v.db.reminders
    await v.save()
    assert v._message_id is not None

    v2 = build_vault(fake)
    await v2.setup()
    assert v2.db.seq == 7
    assert 1 in v2.db.reminders
    assert v2.db.reminders[1].text == "legacy"
    assert v2.db.users[1].verified is True


async def run_all():
    test_db_roundtrip()
    test_index_roundtrip_wrap()
    await test_fresh_setup_and_reload()
    await test_save_small_inline()
    await test_save_large_becomes_document()
    await test_shrink_cleans_up_document()
    await test_migrate_v1()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run_all())
