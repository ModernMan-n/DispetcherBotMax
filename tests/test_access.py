"""Доступ к боту только участникам чата «Сотрудники»."""

from __future__ import annotations

import pytest

from app import staff
from app.membership import MEMBER_TTL, MembershipChecker
from tests.harness import APPLICANT, DISPATCH_CHAT_ID, WORKER, buttons_of

STAFF_CHAT = -888
OUTSIDER = {"user_id": 666, "first_name": "Посторонний", "is_bot": False}
NET_PHONE = "+79001112233"


@pytest.fixture
def staff_chat():
    return STAFF_CHAT


async def test_member_can_use_bot(h, org):
    await h.text("/start")
    assert "a|new" in h.last_buttons()


async def test_outsider_denied_everywhere(h, db, org):
    await h.text("/start", user=OUTSIDER)
    assert h.last_text == org.text("access_denied")
    assert h.last_buttons() == []

    # Кнопки из старого меню тоже не работают
    await h.press("a|new", user=OUTSIDER)
    assert h.callbacks[-1]["notification"] == "Бот доступен только сотрудникам"

    # И войти как специалист нельзя, даже зная номер
    async with db.session() as s, s.begin():
        await staff.add_specialist(s, org.org_id, name="Кто-то", phone=NET_PHONE, directions=["network"])
    await h.contact(user=OUTSIDER, owner=OUTSIDER, phone=NET_PHONE)
    assert h.last_text == org.text("access_denied")
    async with db.session() as s:
        assert await staff.get_specialist(s, org.org_id, OUTSIDER["user_id"]) is None


async def test_api_failure_denies(h, org):
    h.members_api_down = True
    await h.text("/start")
    assert h.last_text == org.text("access_denied")


async def test_result_is_cached(h):
    await h.text("/start")
    await h.text("/my")
    await h.press("a|new")
    assert h.member_checks == 1


async def test_removed_from_staff_chat_loses_access_immediately(h, db, org):
    async with db.session() as s, s.begin():
        await staff.add_specialist(s, org.org_id, name="Пётр", phone=NET_PHONE, directions=["network"])
    await h.text("/staff", user=WORKER)
    await h.contact(user=WORKER, owner=WORKER, phone=NET_PHONE)
    assert "На линии" in h.last_text

    h.staff_members.discard(WORKER["user_id"])
    await h.chat_event("user_removed", chat_id=STAFF_CHAT, user=WORKER)

    # Доступ закрыт сразу, хотя прошлая проверка ещё «свежая» в кэше
    await h.text("/staff", user=WORKER)
    assert h.last_text == org.text("access_denied")
    async with db.session() as s:
        a = await staff.get_specialist(s, org.org_id, WORKER["user_id"])
    assert a.on_duty is False  # снят с линии

    # Новые заявки ему больше не приходят
    await h.submit_request("Нет интернета в кабинете")
    assert not [m for m in h.to_user(WORKER["user_id"]) if m["text"].startswith("Заявка №1")]


async def test_removal_from_other_chat_ignored(h):
    await h.text("/start")
    await h.chat_event("user_removed", chat_id=DISPATCH_CHAT_ID, user=APPLICANT)
    await h.text("/start")
    assert "a|new" in h.last_buttons()


async def test_joined_staff_chat_gets_access(h, org):
    h.staff_members.discard(APPLICANT["user_id"])
    await h.text("/start")
    assert h.last_text == org.text("access_denied")

    h.staff_members.add(APPLICANT["user_id"])
    await h.chat_event("user_added", chat_id=STAFF_CHAT, user=APPLICANT)
    await h.text("/start")
    assert "a|new" in h.last_buttons()


async def test_dispatch_chat_not_gated(h):
    """Кнопки в чате диспетчерской не проверяются: туда людей добавляет администратор чата."""
    await h.submit_request("Нет интернета в кабинете")
    other = {"user_id": 777, "first_name": "Диспетчер", "is_bot": False}
    await h.press_in_chat("r|take|1", user=other)
    assert "В работе — Диспетчер" in h.last_edit.text
    assert buttons_of(h.last_edit.attachments)


async def test_checker_ttl():
    now = [0.0]
    checker = MembershipChecker(-1, clock=lambda: now[0])
    calls = []

    class FakeBot:
        async def get_chat_member(self, chat_id, user_id):
            calls.append(user_id)
            return object()

    bot = FakeBot()
    assert await checker.is_member(bot, 1)
    assert await checker.is_member(bot, 1)
    assert len(calls) == 1
    now[0] += MEMBER_TTL + 1
    assert await checker.is_member(bot, 1)
    assert len(calls) == 2


async def test_disabled_checker_allows_everyone():
    assert await MembershipChecker(None).is_member(object(), 123)
