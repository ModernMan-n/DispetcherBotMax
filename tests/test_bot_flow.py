"""Сквозные сценарии бота на подставных событиях MAX, без сети."""

from __future__ import annotations

import itertools
import logging
from types import SimpleNamespace

import pytest
from maxapi import Bot
from maxapi.methods.types.getted_updates import get_update_model

from app import repo
from app.bot.setup import create_dispatcher
from app.models import Status

APPLICANT = {"user_id": 100, "first_name": "Иван", "last_name": "Тестов", "is_bot": False}
WORKER = {"user_id": 200, "first_name": "Пётр", "last_name": "Мастер", "is_bot": False}
WORKER2 = {"user_id": 201, "first_name": "Анна", "is_bot": False}
BOT_USER = {"user_id": 1, "first_name": "Бот", "is_bot": True}
DIALOG_ID = 500
DISPATCH_CHAT_ID = -777

_seq = itertools.count(1)


def buttons_of(attachments) -> list[str]:
    for att in attachments or []:
        if hasattr(att, "payload") and hasattr(att.payload, "buttons"):
            return [b.payload for row in att.payload.buttons for b in row]
    return []


class Harness:
    def __init__(self, dp, bot: Bot) -> None:
        self.dp, self.bot = dp, bot
        self.sent: list[dict] = []  # исходящие сообщения
        self.callbacks: list[dict] = []  # ответы на нажатия (правки сообщения и всплывашки)

    # --- что ответил бот ---

    def in_dialog(self) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == DIALOG_ID]

    def in_chat(self) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == DISPATCH_CHAT_ID]

    def to_user(self, user_id: int) -> list[dict]:
        return [m for m in self.sent if m["user_id"] == user_id]

    @property
    def last_text(self) -> str:
        return self.in_dialog()[-1]["text"]

    def last_buttons(self) -> list[str]:
        return buttons_of(self.in_dialog()[-1]["attachments"])

    @property
    def last_edit(self):
        return self.callbacks[-1]["message"]

    # --- что делает пользователь ---

    async def _feed(self, raw: dict) -> None:
        await self.dp.handle(await get_update_model(raw, self.bot))

    async def text(self, text: str, *, user=APPLICANT, chat_id=DIALOG_ID, chat_type="dialog") -> None:
        n = next(_seq)
        await self._feed({
            "update_type": "message_created",
            "timestamp": n,
            "message": {
                "sender": user,
                "recipient": {"chat_id": chat_id, "chat_type": chat_type},
                "timestamp": n,
                "body": {"mid": f"m{n}", "seq": n, "text": text},
            },
        })

    async def press(self, payload: str, *, user=APPLICANT, chat_id=DIALOG_ID, chat_type="dialog") -> None:
        n = next(_seq)
        await self._feed({
            "update_type": "message_callback",
            "timestamp": n,
            "callback": {"timestamp": n, "callback_id": f"cb{n}", "payload": payload, "user": user},
            "message": {
                "sender": BOT_USER,
                "recipient": {"chat_id": chat_id, "chat_type": chat_type},
                "timestamp": n,
                "body": {"mid": f"b{n}", "seq": n, "text": "Сообщение с кнопками"},
            },
        })

    async def press_in_chat(self, payload: str, user=WORKER) -> None:
        await self.press(payload, user=user, chat_id=DISPATCH_CHAT_ID, chat_type="chat")

    async def submit_request(self, description="Не работает проектор, не включается") -> None:
        await self.press("a|new")
        await self.press("b|2")
        await self.text("214")
        await self.text(description)
        await self.press("a|send")


@pytest.fixture
async def h(db, org, monkeypatch, caplog):
    org = org.model_copy(update={"dispatcher_chat_id": DISPATCH_CHAT_ID})
    bot = Bot(token="test", auto_requests=False, auto_check_subscriptions=False)
    harness = None

    async def fake_send_message(chat_id=None, user_id=None, text=None, attachments=None, **kw):
        harness.sent.append({"chat_id": chat_id, "user_id": user_id, "text": text, "attachments": attachments})

    async def fake_send_callback(callback_id, message=None, notification=None, **kw):
        # Как настоящий MAX: пустой ответ на callback — ошибка 400.
        if message is None and notification is None:
            raise AssertionError("MAX: `message` or `notification` required")
        harness.callbacks.append({"message": message, "notification": notification})

    async def fake_get_me():
        return SimpleNamespace(username="test_bot", first_name="Бот", user_id=1)

    monkeypatch.setattr(bot, "send_message", fake_send_message)
    monkeypatch.setattr(bot, "send_callback", fake_send_callback)
    monkeypatch.setattr(bot, "get_me", fake_get_me)

    dp = create_dispatcher(org, db)
    await dp.startup(bot)
    harness = Harness(dp, bot)
    yield harness
    # Dispatcher.handle глотает исключения хендлеров и пишет их в лог — проверяем, что их не было.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, errors[0].getMessage()


# --- Заявитель ---


async def test_happy_path(h, db, org):
    await h.text("/start")
    assert "диспетчерской" in h.last_text
    assert "a|new" in h.last_buttons()

    await h.press("a|new")
    assert h.last_text == org.text("ask_building")
    assert "b|2" in h.last_buttons()

    await h.press("b|2")
    assert h.last_text == org.text("ask_room")

    await h.text("214")
    assert h.last_text == org.text("ask_description")

    await h.text("Не работает проектор, не включается")
    assert "Корпус 2" in h.last_text and "Компьютерная техника" in h.last_text
    assert "a|send" in h.last_buttons()

    await h.press("a|send")
    assert h.last_text.startswith("Заявка №1 принята")

    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert (req.building, req.room, req.category, req.status) == ("2", "214", "computers", Status.NEW)
    assert req.applicant_max_id == 100 and req.applicant_name == "Иван Тестов"

    # Повторное нажатие «Отправить» не создаёт дубль
    await h.press("a|send")
    assert h.callbacks[-1]["notification"] == "Эта кнопка уже неактуальна"
    async with db.session() as s:
        assert len(await repo.list_active_for_applicant(s, org.org_id, 100)) == 1

    await h.text("/my")
    assert "№1" in h.last_text and "ожидает" in h.last_text


async def test_cancel_midway(h, org):
    await h.press("a|new")
    await h.press("b|1")
    await h.text("/cancel")
    assert h.last_text == org.text("cancelled")
    await h.text("214")  # после отмены текст не считается кабинетом
    assert h.last_text == org.text("idle")


async def test_validation(h, org):
    await h.press("a|new")
    await h.press("b|1")
    await h.text("кабинет на третьем этаже рядом с лестницей")
    assert h.last_text == org.text("bad_room")
    await h.text("301а")
    await h.text("ой")
    assert h.last_text == org.text("bad_description")


async def test_unknown_building_rejected(h):
    await h.press("a|new")
    await h.press("b|999")
    assert h.callbacks[-1]["notification"] == "Такого корпуса нет, выберите из списка"


# --- Чат диспетчерской ---


async def test_new_request_posted_to_dispatch_chat(h):
    await h.submit_request()
    cards = h.in_chat()
    assert len(cards) == 1
    assert "Заявка №1 · Компьютерная техника" in cards[0]["text"]
    assert "Корпус 2, каб. 214" in cards[0]["text"]
    assert "От: Иван Тестов" in cards[0]["text"]
    assert "r|take|1" in buttons_of(cards[0]["attachments"])


async def test_take_and_done_notify_applicant(h, db, org):
    await h.submit_request()

    await h.press_in_chat("r|take|1")
    assert "В работе — Пётр Мастер" in h.last_edit.text
    assert "r|done|1" in buttons_of(h.last_edit.attachments)
    assert h.to_user(100)[-1]["text"] == org.text("notify_in_progress", id=1)

    await h.press_in_chat("r|done|1")
    assert "Выполнена — Пётр Мастер, за" in h.last_edit.text
    assert buttons_of(h.last_edit.attachments) == []
    assert h.to_user(100)[-1]["text"] == org.text("notify_done", id=1)

    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert req.status == Status.DONE
    assert req.assignee.max_id == 200 and req.assignee.name == "Пётр Мастер"
    assert [e.status for e in req.log] == ["new", "in_progress", "done"]
    assert [e.changed_by for e in req.log] == [100, 200, 200]


async def test_second_take_does_not_steal(h, db, org):
    await h.submit_request()
    await h.press_in_chat("r|take|1", user=WORKER)
    await h.press_in_chat("r|take|1", user=WORKER2)  # нажала по старой карточке
    assert "Пётр Мастер" in h.last_edit.text
    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert req.assignee.max_id == 200


async def test_release_returns_to_queue(h, db, org):
    await h.submit_request()
    await h.press_in_chat("r|take|1")
    await h.press_in_chat("r|release|1")
    assert "Новая" in h.last_edit.text
    assert "r|take|1" in buttons_of(h.last_edit.attachments)
    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert req.status == Status.NEW and req.assignee_id is None


async def test_cancel_needs_confirmation(h, db, org):
    await h.submit_request()
    await h.press_in_chat("r|cancel|1")
    assert buttons_of(h.last_edit.attachments) == ["r|cancel_ok|1", "r|back|1"]
    await h.press_in_chat("r|back|1")
    assert "r|take|1" in buttons_of(h.last_edit.attachments)

    await h.press_in_chat("r|cancel|1")
    await h.press_in_chat("r|cancel_ok|1")
    assert "Отменена" in h.last_edit.text
    assert h.to_user(100)[-1]["text"] == org.text("notify_cancelled", id=1)


async def test_manual_category(h, db, org):
    await h.submit_request()
    await h.press_in_chat("r|cat|1")
    assert "rc|1|network" in buttons_of(h.last_edit.attachments)
    await h.press_in_chat("rc|1|network")
    assert "Сеть и интернет" in h.last_edit.text
    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert (req.category, req.category_source) == ("network", "manual")


async def test_dispatch_buttons_ignored_outside_dispatch_chat(h, db, org):
    await h.submit_request()
    # Тот же payload, но из личного диалога — не должен менять статус
    await h.press("r|take|1", user=WORKER)
    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert req.status == Status.NEW


async def test_group_chat_messages_not_answered(h):
    before = len(h.sent)
    await h.text("Всем привет", user=WORKER, chat_id=DISPATCH_CHAT_ID, chat_type="chat")
    assert len(h.sent) == before
