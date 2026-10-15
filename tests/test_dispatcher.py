"""Диспетчер: сроки, приоритет, категории, правка заявок, история; вход по ссылке-приглашению."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import categories, repo, staff, stats
from app.models import Category, Priority, Role, utcnow
from tests.harness import WORKER
from tests.test_web import APPLICANT_MAX, client, login, staff_chat_id, web  # noqa: F401 — фикстуры

DISP_PHONE, DISP_MAX = "+79000000009", 1009


@pytest.fixture
async def disp(web):  # noqa: F811
    async with web.db.session() as s, s.begin():
        a = await staff.add_specialist(
            s, web.org.org_id, name="Диспетчер Дарья", phone=DISP_PHONE, directions=["network"], role=Role.DISPATCHER,
        )
        a.max_id = DISP_MAX
    web.bot.staff_members.add(DISP_MAX)
    async with await login(web, DISP_PHONE, DISP_MAX) as c:
        yield c


async def _request(web):
    async with web.db.session() as s:
        return await repo.get_request(s, web.org.org_id, 1)


# --- права диспетчера ---


async def test_dispatcher_permissions(web, disp):  # noqa: F811
    me = (await disp.get("/api/me")).json()
    assert me["role"] == "dispatcher" and me["role_title"] == "Диспетчер"
    assert {"admin_requests", "manage_categories", "work"} <= set(me["perms"])
    assert "manage_staff" not in me["perms"]
    assert (await disp.get("/api/staff")).status_code == 403
    assert (await disp.get("/api/categories")).status_code == 200


async def test_specialist_cannot_manage_due_priority_categories(web):  # noqa: F811
    async with await login(web, "+79000000002", 1002) as spec:
        assert (await spec.post("/api/requests/1/priority", json={"priority": "urgent"})).status_code == 403
        assert (await spec.post("/api/requests/1/due", json={"shift_hours": 24})).status_code == 403
        assert (await spec.get("/api/categories")).status_code == 403


# --- приоритет ---


async def test_priority(web, disp):  # noqa: F811
    r = await disp.post("/api/requests/1/priority", json={"priority": "urgent"})
    assert r.status_code == 200 and r.json()["priority"] == "urgent" and r.json()["priority_title"] == "срочный"
    assert (await disp.post("/api/requests/1/priority", json={"priority": "nonsense"})).status_code == 422

    # Срочные — первыми в списке
    async with web.db.session() as s, s.begin():
        await repo.create_request(s, org_id=web.org.org_id, building="1", room="1", description="Вторая заявка",
                                  category="network", applicant_max_id=2, applicant_name="Б")
    ids = [i["id"] for i in (await disp.get("/api/requests")).json()["items"]]
    assert ids[0] == 1
    await disp.post("/api/requests/1/priority", json={"priority": "low"})
    ids = [i["id"] for i in (await disp.get("/api/requests")).json()["items"]]
    assert ids[0] == 2


# --- сроки ---


async def test_due_shift_absolute_reset(web, disp):  # noqa: F811
    req = await _request(web)
    normative = req.created_at + timedelta(hours=web.org.category_sla["network"])

    r = (await disp.post("/api/requests/1/due", json={"shift_hours": 24, "comment": "ждём запчасть"})).json()
    req = await _request(web)
    assert req.due_manual and req.due_at > normative

    future = (utcnow() + timedelta(days=5)).astimezone().strftime("%Y-%m-%dT%H:%M")
    r = await disp.post("/api/requests/1/due", json={"due_at": future})
    assert r.status_code == 200, r.text

    past = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    r = await disp.post("/api/requests/1/due", json={"due_at": past})
    assert r.status_code == 409 and "прошёл" in r.json()["detail"]

    r = await disp.post("/api/requests/1/due", json={"reset": True})
    req = await _request(web)
    assert not req.due_manual and req.due_at == normative

    history = (await disp.get("/api/requests/1/history")).json()["items"]
    texts = [h["text"] for h in history]
    assert any("Причина: ждём запчасть" in t for t in texts)
    assert any("по нормативу категории" in t for t in texts)
    assert history[0]["text"] == "Создана"


async def test_due_change_notifies_assignee(web, disp):  # noqa: F811
    async with await login(web, "+79000000002", 1002) as spec:
        await spec.post("/api/requests/1/action", json={"action": "take"})
    await disp.post("/api/requests/1/due", json={"shift_hours": 48})
    assert any("Срок по заявке №1 изменён" in t for t in web.bot.to(1002))


async def test_extended_due_is_not_overdue(web, disp):  # noqa: F811
    """Просрочка считается от срока: продлили — заявка больше не просрочена."""
    async with web.db.session() as s, s.begin():
        req = await repo.get_request(s, web.org.org_id, 1)
        req.created_at = utcnow() - timedelta(days=3)
        req.due_at = req.created_at + timedelta(hours=4)
    async with web.db.session() as s:
        assert (await stats.collect(s, web.org, days=7))["now"]["overdue"] == 1
    item = (await disp.get("/api/requests")).json()["items"][0]
    assert item["overdue"] is True

    await disp.post("/api/requests/1/due", json={"shift_hours": 24})
    async with web.db.session() as s:
        assert (await stats.collect(s, web.org, days=7))["now"]["overdue"] == 0
    assert (await disp.get("/api/requests")).json()["items"][0]["overdue"] is False


async def test_category_change_recomputes_due_unless_manual(web, disp):  # noqa: F811
    req = await _request(web)
    await disp.post("/api/requests/1/category", json={"category": "furniture"})
    req2 = await _request(web)
    assert req2.due_at == req.created_at + timedelta(hours=web.org.category_sla["furniture"])

    await disp.post("/api/requests/1/due", json={"shift_hours": 1})
    manual = (await _request(web)).due_at
    await disp.post("/api/requests/1/category", json={"category": "network"})
    assert (await _request(web)).due_at == manual  # ручной срок не трогаем


# --- правка и контакт заявителя ---


async def test_dispatcher_edits_request_with_history(web, disp):  # noqa: F811
    r = await disp.patch("/api/requests/1", json={"room": "305", "description": "Нет интернета у окна"})
    assert r.status_code == 200 and r.json()["room"] == "305"
    texts = [h["text"] for h in (await disp.get("/api/requests/1/history")).json()["items"]]
    assert any("кабинет: 214 → 305" in t and "описание проблемы" in t for t in texts)


async def test_contact_applicant_sends_link_to_me(web, disp):  # noqa: F811
    r = await disp.post("/api/requests/1/contact", json={})
    assert r.status_code == 200
    sent = [m for m in web.bot.sent if m["user_id"] == DISP_MAX and "Заявитель по заявке №1" in m["text"]]
    assert sent and f'href="max://user/{APPLICANT_MAX}"' in sent[0]["text"]


# --- категории ---


async def test_categories_crud(web, disp):  # noqa: F811
    items = (await disp.get("/api/categories")).json()["items"]
    assert {i["code"] for i in items} >= {"network", "other"}
    assert next(i for i in items if i["code"] == "network")["requests"] == 1

    r = await disp.post("/api/categories", json={"title": "Видеонаблюдение", "sla_hours": 12, "keywords": ["Камер", "видео"]})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert r.json()["keywords"] == ["камер", "видео"]
    assert (await disp.post("/api/categories", json={"title": "видеонаблюдение", "sla_hours": 1})).status_code == 409

    # Новая категория сразу видна в справочнике
    meta = (await disp.get("/api/meta")).json()
    assert any(c["code"] == code and c["title"] == "Видеонаблюдение" for c in meta["categories"])

    r = await disp.patch(f"/api/categories/{code}", json={"sla_hours": 6, "title": "Камеры"})
    assert r.json()["sla_hours"] == 6 and r.json()["title"] == "Камеры"
    assert (await disp.delete(f"/api/categories/{code}")).json() == {"deleted": True, "archived": False}

    # Категорию с заявками — только отключаем
    assert (await disp.delete("/api/categories/network")).json() == {"deleted": False, "archived": True}
    # «По умолчанию» не трогаем
    assert (await disp.delete("/api/categories/other")).status_code == 409
    assert (await disp.patch("/api/categories/other", json={"active": False})).status_code == 409


async def test_inactive_category_cannot_be_chosen(web, disp):  # noqa: F811
    await disp.patch("/api/categories/furniture", json={"active": False})
    assert (await disp.post("/api/requests/1/category", json={"category": "furniture"})).status_code == 409


async def test_config_seeds_only_once(db, org):
    """Конфиг — только начальный набор: правки диспетчера в БД он не перезаписывает."""
    async with db.session() as s, s.begin():
        row = await s.scalar(Category.__table__.select().where(Category.code == "network"))
        assert row is not None
        await categories.update(s, org.org_id, "network", {"title": "Сеть", "keywords": ["сеть"]})
        await categories.seed_from_config(s, org)  # как при следующем запуске
    await categories.refresh(db, org, force=True)
    assert org.category_titles["network"] == "Сеть"


async def test_bot_uses_new_category_keywords(h, db, org):
    """Категорию добавили в дашборде — бот сразу определяет по её ключевым словам."""
    async with db.session() as s, s.begin():
        await categories.create(s, org.org_id, title="Видеонаблюдение", sla_hours=12, keywords=["камер"])
    await categories.refresh(db, org, force=True)
    await h.submit_request("Не работает камера в коридоре")
    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, 1)
    assert org.category_titles[req.category] == "Видеонаблюдение"
    assert req.due_at is not None and req.due_at - req.created_at == timedelta(hours=12)


# --- карточка и вход по ссылке ---


async def test_card_shows_priority_and_due(h, db, org):
    await h.submit_request("Нет интернета в кабинете")
    card = h.in_chat()[0]["text"]
    assert "Срок: до " in card
    async with db.session() as s, s.begin():
        req = await repo.get_request(s, org.org_id, 1)
        req.priority = Priority.URGENT
        req.due_at = datetime(2020, 1, 1, tzinfo=UTC)
    await h.press_in_chat("r|back|1")
    assert "🔴" in h.last_edit.text and "просрочена" in h.last_edit.text and "срочный" in h.last_edit.text


async def test_staff_invite_link_opens_login(h):
    await h.start("staff", user=WORKER)
    assert "Поделиться номером" in str(h.in_dialog()[-1]["attachments"])


async def test_login_page_shows_bot_link(web):  # noqa: F811
    async with client() as c:
        assert (await c.get("/api/public")).json() == {"staff_link": "https://max.ru/dispatch_test_bot?start=staff"}


# --- уведомления заявителю ---


async def test_applicant_notified_about_priority_due_and_assignment(web, disp):  # noqa: F811
    await disp.post("/api/requests/1/priority", json={"priority": "urgent"})
    assert web.bot.to(APPLICANT_MAX)[-1] == "Приоритет заявки №1 изменён: срочный."

    await disp.post("/api/requests/1/due", json={"shift_hours": 24, "comment": "внутренняя причина"})
    note = web.bot.to(APPLICANT_MAX)[-1]
    assert note.startswith("Срок выполнения заявки №1 изменён: до ")
    assert "причина" not in note  # причину переноса заявителю не показываем

    spec_id = next(a["id"] for a in (await disp.get("/api/meta")).json()["assignees"] if a["name"] == "Пётр Сетевик")
    await disp.post("/api/requests/1/assign", json={"assignee_id": spec_id})
    assert web.bot.to(APPLICANT_MAX)[-1] == (
        'Ваша заявка №1 взята в работу. Исполнитель: <a href="max://user/1002">Пётр Сетевик</a>'
    )

    me = (await disp.get("/api/me")).json()["id"]
    await disp.post("/api/requests/1/assign", json={"assignee_id": me})
    assert web.bot.to(APPLICANT_MAX)[-1] == (
        f'По заявке №1 назначен другой исполнитель: <a href="max://user/{DISP_MAX}">Диспетчер Дарья</a>'
    )


async def test_unchanged_priority_sends_nothing(web, disp):  # noqa: F811
    await disp.post("/api/requests/1/priority", json={"priority": "normal"})
    assert web.bot.to(APPLICANT_MAX) == []
