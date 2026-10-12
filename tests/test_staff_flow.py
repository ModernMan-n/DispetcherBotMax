"""Специалисты: вход по номеру, линия, уведомления в личку, карточки и их синхронизация."""

from __future__ import annotations

import pytest

from app import repo, staff
from app.models import Status
from tests.harness import APPLICANT, DISPATCH_CHAT_ID, WORKER, WORKER2, buttons_of

# WORKER — сетевик/айтишник, WORKER2 — сантехник
NET_PHONE = "+7 (900) 111-22-33"
PLUMB_PHONE = "89002223344"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (900) 111-22-33", "79001112233"),
        ("8 900 111 22 33", "79001112233"),
        ("9001112233", "79001112233"),
        ("12345", None),
        ("", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert staff.normalize_phone(raw) == expected


@pytest.fixture
async def specialists(db, org):
    async with db.session() as s, s.begin():
        net = await staff.add_specialist(
            s, org.org_id, name="Пётр Сетевик", phone=NET_PHONE, directions=["network", "computers"]
        )
        plumb = await staff.add_specialist(
            s, org.org_id, name="Анна Сантехник", phone=PLUMB_PHONE, directions=["plumbing"]
        )
    return net, plumb


async def login(h, user, phone):
    await h.text("/staff", user=user)
    await h.contact(user=user, owner=user, phone=phone)


async def test_add_rejects_duplicate_phone(db, org, specialists):
    async with db.session() as s:
        with pytest.raises(staff.StaffError):
            await staff.add_specialist(s, org.org_id, name="Дубль", phone="8-900-111-22-33", directions=["network"])


# --- вход ---


async def test_login_by_own_contact(h, db, org, specialists):
    await h.text("/staff", user=WORKER)
    assert "Поделиться номером" in str(h.in_dialog()[-1]["attachments"])

    await h.contact(user=WORKER, owner=WORKER, phone=NET_PHONE)
    assert "Пётр Сетевик" in h.last_text and "На линии" in h.last_text
    assert "Сеть и интернет, Компьютерная техника" in h.last_text
    assert "s|mine" in h.last_buttons() and "s|duty_off" in h.last_buttons()

    async with db.session() as s:
        a = await staff.get_specialist(s, org.org_id, WORKER["user_id"])
    assert a is not None and a.on_duty

    # /start у специалиста — меню специалиста
    await h.text("/start", user=WORKER)
    assert "s|all" in h.last_buttons()


async def test_foreign_contact_rejected(h, db, org, specialists):
    # Прислал чужой контакт (пересланный), а не свой
    await h.contact(user=APPLICANT, owner=WORKER, phone=NET_PHONE)
    assert "собственный номер" in h.last_text
    async with db.session() as s:
        assert await staff.get_specialist(s, org.org_id, APPLICANT["user_id"]) is None


async def test_unknown_phone_rejected(h, specialists):
    await h.contact(user=APPLICANT, owner=APPLICANT, phone="+79990000000")
    assert "не найден среди сотрудников" in h.last_text


async def test_phone_already_linked_to_other_account(h, specialists):
    await login(h, WORKER, NET_PHONE)
    other = {**APPLICANT, "user_id": 999}
    await h.contact(user=other, owner=other, phone=NET_PHONE)
    assert "привязан к другому аккаунту" in h.last_text


async def test_regular_user_has_no_staff_menu(h, specialists):
    await h.text("/start")
    assert "s|mine" not in h.last_buttons()
    await h.press("s|all")
    assert h.callbacks[-1]["notification"] == "Доступно только специалистам"


# --- уведомления ---


async def test_new_request_goes_to_on_duty_specialists_of_direction(h, specialists):
    await login(h, WORKER, NET_PHONE)
    await login(h, WORKER2, PLUMB_PHONE)
    await h.submit_request("Нет интернета в кабинете")

    net_cards = [m for m in h.to_user(WORKER["user_id"]) if m["text"].startswith("Заявка №1")]
    assert len(net_cards) == 1
    assert "r|take|1" in buttons_of(net_cards[0]["attachments"])
    # Сантехнику заявка по сети не приходит
    assert not [m for m in h.to_user(WORKER2["user_id"]) if m["text"].startswith("Заявка №1")]
    # В чат диспетчерской — как и раньше
    assert len(h.in_chat()) == 1


async def test_off_duty_specialist_not_notified(h, specialists):
    await login(h, WORKER, NET_PHONE)
    await h.press("s|duty_off", user=WORKER)
    assert "Не на линии" in h.last_edit.text
    assert "s|duty_on" in buttons_of(h.last_edit.attachments)

    await h.submit_request("Нет интернета в кабинете")
    assert not [m for m in h.to_user(WORKER["user_id"]) if m["text"].startswith("Заявка №1")]

    await h.press("s|duty_on", user=WORKER)
    assert "На линии" in h.last_edit.text


# --- работа с карточкой в личке ---


async def test_take_from_dm_syncs_all_cards(h, db, org, specialists):
    await login(h, WORKER, NET_PHONE)
    await h.submit_request("Нет интернета в кабинете")
    dm_card = next(m for m in h.to_user(WORKER["user_id"]) if m["text"].startswith("Заявка №1"))
    chat_card = h.in_chat()[0]

    await h.press("r|take|1", user=WORKER, mid=dm_card["mid"])
    assert "В работе — Пётр Сетевик" in h.last_edit.text

    # Карточка в чате диспетчерской обновилась сама
    assert "В работе — Пётр Сетевик" in h.message(chat_card["mid"])["text"]
    # Нажатую карточку правит сам callback, через edit_message её повторно не трогаем
    assert all(e["message_id"] != dm_card["mid"] for e in h.edits)

    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
        sp = await staff.get_specialist(s, org.org_id, WORKER["user_id"])
    assert req.assignee_id == sp.id  # заявка записана на карточку специалиста

    # Закрытие в чате диспетчерской обновляет карточку в личке
    await h.press_in_chat("r|done|1", user=WORKER2, mid=chat_card["mid"])
    assert "Выполнена" in h.message(dm_card["mid"])["text"]
    assert buttons_of(h.message(dm_card["mid"])["attachments"]) == []


async def test_lists_and_open_card(h, specialists):
    await login(h, WORKER, NET_PHONE)
    await h.submit_request("Нет интернета в кабинете")  # №1, сеть
    await h.submit_request("Течёт кран в туалете")  # №2, сантехника

    await h.press("s|mine", user=WORKER)
    lists = [m for m in h.in_dialog() if "Активные заявки ваших направлений" in m["text"]]
    assert "№1" in lists[-1]["text"] and "№2" not in lists[-1]["text"]
    assert buttons_of(lists[-1]["attachments"]) == ["r|open|1"]

    await h.press("s|all", user=WORKER)
    lists = [m for m in h.in_dialog() if "Все активные заявки" in m["text"]]
    assert "№1" in lists[-1]["text"] and "№2" in lists[-1]["text"]

    # Открыть заявку чужого направления и взять её — специалисту можно
    before = len(h.sent)
    await h.press("r|open|2", user=WORKER)
    card = h.sent[before]
    assert card["text"].startswith("Заявка №2") and "r|take|2" in buttons_of(card["attachments"])


async def test_category_change_notifies_new_direction(h, specialists):
    await login(h, WORKER2, PLUMB_PHONE)
    await h.submit_request("Нет интернета в кабинете")  # ушла сетевикам, сантехник не получил
    assert not [m for m in h.to_user(WORKER2["user_id"]) if m["text"].startswith("Заявка №1")]

    await h.press_in_chat("r|cat|1", user=WORKER)
    await h.press_in_chat("rc|1|plumbing", user=WORKER)
    cards = [m for m in h.to_user(WORKER2["user_id"]) if m["text"].startswith("Заявка №1")]
    assert len(cards) == 1 and "Сантехника" in cards[0]["text"]


async def test_non_specialist_cannot_use_cards_in_dm(h, db, org, specialists):
    await h.submit_request("Нет интернета в кабинете")
    await h.press("r|take|1")  # обычный заявитель в личке
    async with db.session() as s:
        assert (await repo.get_request(s, org.org_id, 1)).status == Status.NEW


async def test_deactivated_specialist_loses_access(h, db, org, specialists):
    await login(h, WORKER, NET_PHONE)
    async with db.session() as s, s.begin():
        a = await staff.get_specialist(s, org.org_id, WORKER["user_id"])
        a.active = False
    await h.submit_request("Нет интернета в кабинете")
    assert not [m for m in h.to_user(WORKER["user_id"]) if m["text"].startswith("Заявка №1")]
    await h.press("s|mine", user=WORKER)
    assert h.callbacks[-1]["notification"] == "Доступно только специалистам"


def test_chat_constant_is_group():
    assert DISPATCH_CHAT_ID < 0
