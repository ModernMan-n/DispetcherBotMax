from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from maxapi import Bot

from app.bot.setup import create_dispatcher
from app.config import load_org_config
from app.db import open_db
from tests.harness import DISPATCH_CHAT_ID, Harness


@pytest.fixture
def org():
    return load_org_config("config/org.yaml")


@pytest.fixture
async def db(org):
    database = await open_db("sqlite+aiosqlite:///:memory:", org)
    yield database
    await database.close()


@pytest.fixture
async def h(db, org, monkeypatch, caplog):
    org = org.model_copy(update={"dispatcher_chat_id": DISPATCH_CHAT_ID})
    bot = Bot(token="test", auto_requests=False, auto_check_subscriptions=False)
    harness = None

    async def fake_send_message(chat_id=None, user_id=None, text=None, attachments=None, **kw):
        mid = f"out{len(harness.sent) + 1}"
        harness.sent.append(
            {"mid": mid, "chat_id": chat_id, "user_id": user_id, "text": text, "attachments": attachments}
        )
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=mid)))

    async def fake_edit_message(message_id, text=None, attachments=None, **kw):
        harness.edits.append({"message_id": message_id, "text": text, "attachments": attachments})

    async def fake_send_callback(callback_id, message=None, notification=None, **kw):
        # Как настоящий MAX: пустой ответ на callback — ошибка 400.
        if message is None and notification is None:
            raise AssertionError("MAX: `message` or `notification` required")
        harness.callbacks.append({"message": message, "notification": notification})

    async def fake_get_me():
        return SimpleNamespace(username="test_bot", first_name="Бот", user_id=1)

    monkeypatch.setattr(bot, "send_message", fake_send_message)
    monkeypatch.setattr(bot, "send_callback", fake_send_callback)
    monkeypatch.setattr(bot, "edit_message", fake_edit_message)
    monkeypatch.setattr(bot, "get_me", fake_get_me)

    dp = create_dispatcher(org, db)
    await dp.startup(bot)
    harness = Harness(dp, bot)
    yield harness
    # Dispatcher.handle глотает исключения хендлеров и пишет их в лог — проверяем, что их не было.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, errors[0].getMessage()
