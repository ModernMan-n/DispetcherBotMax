"""Сквозные сценарии бота на подставных событиях MAX, без сети."""

from __future__ import annotations

from app import repo
from app.models import Status
from tests.harness import DISPATCH_CHAT_ID, WORKER, WORKER2, buttons_of

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
    # Заявитель — ссылка на профиль MAX
    assert 'От: <a href="max://user/100">Иван Тестов</a>' in cards[0]["html"]
    assert cards[0]["format"] == "html"
    assert "r|take|1" in buttons_of(cards[0]["attachments"])


async def test_take_and_done_notify_applicant(h, db, org):
    await h.submit_request()

    await h.press_in_chat("r|take|1")
    assert "В работе — Пётр Мастер" in h.last_edit.text
    assert h.last_edit.format == "html" and 'href="max://user/100"' in h.last_edit.text
    assert "r|done|1" in buttons_of(h.last_edit.attachments)
    note = h.to_user(100)[-1]
    assert note["text"] == "Ваша заявка №1 взята в работу. Исполнитель: Пётр Мастер"
    assert note["format"] == "html" and 'href="max://user/200"' in note["html"]  # исполнитель — ссылкой

    await h.press_in_chat("r|done|1")
    assert "Выполнена — Пётр Мастер, за" in h.last_edit.text and "работы" in h.last_edit.text
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


async def test_card_escapes_user_text(h):
    """Описание с HTML-символами не ломает карточку и не превращается в разметку."""
    await h.submit_request('Не работает <b>проектор</b> & <a href="x">ссылка</a>')
    card = h.in_chat()[0]
    assert "&lt;b&gt;проектор&lt;/b&gt; &amp;" in card["html"]
    assert 'Не работает <b>проектор</b> & <a href="x">ссылка</a>' in card["text"]
    assert card["html"].count("<a ") == 1  # единственная ссылка — на заявителя


async def test_release_notifies_applicant(h, org):
    await h.submit_request()
    await h.press_in_chat("r|take|1")
    await h.press_in_chat("r|release|1")
    assert h.to_user(100)[-1]["text"] == org.text("notify_released", id=1)


async def test_no_notification_about_own_actions(h, db, org):
    """Заявитель сам взял свою заявку (например, диспетчер) — уведомлять его не о чем."""
    await h.submit_request()
    before = len(h.to_user(100))
    await h.press_in_chat("r|take|1", user={"user_id": 100, "first_name": "Иван", "last_name": "Тестов", "is_bot": False})
    assert len(h.to_user(100)) == before
