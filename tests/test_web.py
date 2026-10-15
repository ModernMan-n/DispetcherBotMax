"""Веб-интерфейс: вход по коду из MAX, права ролей, заявки, специалисты."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import repo, staff
from app.dashboard.web import app
from app.models import Role, Status

ADMIN_PHONE, SPEC_PHONE, VIEWER_PHONE, UNLINKED_PHONE = "+79000000001", "+79000000002", "+79000000003", "+79000000004"
ADMIN_MAX, SPEC_MAX, VIEWER_MAX = 1001, 1002, 1003
APPLICANT_MAX = 500
JSON = {"content-type": "application/json"}


class FakeBot:
    """Подставной MAX: запоминает отправленное, отвечает о членстве в чате сотрудников."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[str] = []
        self.staff_members = {ADMIN_MAX, SPEC_MAX, VIEWER_MAX}

    async def send_message(self, chat_id=None, user_id=None, text=None, attachments=None, **kw):
        self.sent.append({"chat_id": chat_id, "user_id": user_id, "text": text})
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=f"m{len(self.sent)}")))

    async def edit_message(self, message_id, **kw):
        self.edits.append(message_id)

    async def get_me(self):
        return SimpleNamespace(username="dispatch_test_bot")

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(user_id=user_id) if user_id in self.staff_members else None

    def to(self, user_id):
        return [m["text"] for m in self.sent if m["user_id"] == user_id]

    def last_code(self, user_id) -> str:
        return re.search(r"\d{6}", self.to(user_id)[-1]).group()


@pytest.fixture
def staff_chat_id():
    return None


@pytest.fixture
async def web(tmp_path, monkeypatch, staff_chat_id):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setenv("MAX_BOT_TOKEN", "test")
    monkeypatch.delenv("DISPATCHER_CHAT_ID", raising=False)
    if staff_chat_id:
        monkeypatch.setenv("STAFF_CHAT_ID", str(staff_chat_id))
    else:
        monkeypatch.delenv("STAFF_CHAT_ID", raising=False)
    async with app.router.lifespan_context(app):
        bot = FakeBot()
        app.state.bot = bot
        org = app.state.org
        async with app.state.db.session() as s, s.begin():
            for name, phone, role, max_id in [
                ("Админ Админов", ADMIN_PHONE, Role.ADMIN, ADMIN_MAX),
                ("Пётр Сетевик", SPEC_PHONE, Role.SPECIALIST, SPEC_MAX),
                ("Директор", VIEWER_PHONE, Role.VIEWER, VIEWER_MAX),
                ("Не входил", UNLINKED_PHONE, Role.SPECIALIST, None),
            ]:
                a = await staff.add_specialist(s, org.org_id, name=name, phone=phone, directions=["network"], role=role)
                a.max_id = max_id
            await repo.create_request(
                s, org_id=org.org_id, building="1", room="214", description="Нет интернета",
                category="network", applicant_max_id=APPLICANT_MAX, applicant_name="Иван Заявитель",
            )
        yield SimpleNamespace(bot=bot, org=org, db=app.state.db)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"host": "test"})


class LoggedIn:
    """Клиент, уже вошедший по коду. Использовать как `async with await login(...) as c`."""

    def __init__(self, c: AsyncClient):
        self.c = c

    async def __aenter__(self) -> AsyncClient:
        return self.c

    async def __aexit__(self, *exc) -> None:
        await self.c.aclose()

    def __getattr__(self, name):
        return getattr(self.c, name)


async def login(web, phone: str, max_id: int) -> LoggedIn:
    c = client()
    r = await c.post("/api/auth/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    r = await c.post("/api/auth/verify", json={"phone": phone, "code": web.bot.last_code(max_id)})
    assert r.status_code == 200, r.text
    return LoggedIn(c)


# --- вход ---


async def test_pages_require_login(web):
    async with client() as c:
        assert (await c.get("/api/stats")).status_code == 401
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert (await c.get("/login")).status_code == 200


async def test_login_flow(web):
    async with client() as c:
        r = await c.post("/api/auth/request", json={"phone": "8 (900) 000-00-01"})
        assert r.status_code == 200
        assert len(web.bot.to(ADMIN_MAX)) == 1  # код пришёл в личку MAX

        bad = await c.post("/api/auth/verify", json={"phone": ADMIN_PHONE, "code": "000000"})
        assert bad.status_code == 401
        ok = await c.post("/api/auth/verify", json={"phone": ADMIN_PHONE, "code": web.bot.last_code(ADMIN_MAX)})
        assert ok.status_code == 200 and ok.json()["role"] == "admin"
        assert "httponly" in ok.headers["set-cookie"].lower()

        me = await c.get("/api/me")
        assert me.json()["name"] == "Админ Админов"

        await c.post("/api/auth/logout", json={})
        assert (await c.get("/api/me")).status_code == 401


async def test_same_answer_for_unknown_and_unlinked_phones(web):
    async with client() as c:
        known = (await c.post("/api/auth/request", json={"phone": SPEC_PHONE})).json()
        unknown = (await c.post("/api/auth/request", json={"phone": "+79991234567"})).json()
        unlinked = (await c.post("/api/auth/request", json={"phone": UNLINKED_PHONE})).json()
    assert known == unknown == unlinked
    assert len(web.bot.sent) == 1  # код получил только привязанный сотрудник


async def test_code_single_use_and_cooldown(web):
    async with client() as c:
        await c.post("/api/auth/request", json={"phone": SPEC_PHONE})
        code = web.bot.last_code(SPEC_MAX)
        assert (await c.post("/api/auth/request", json={"phone": SPEC_PHONE})).status_code == 429
        assert (await c.post("/api/auth/verify", json={"phone": SPEC_PHONE, "code": code})).status_code == 200
        assert (await c.post("/api/auth/verify", json={"phone": SPEC_PHONE, "code": code})).status_code == 401


async def test_attempts_limit(web):
    async with client() as c:
        await c.post("/api/auth/request", json={"phone": SPEC_PHONE})
        code = web.bot.last_code(SPEC_MAX)
        for _ in range(5):
            await c.post("/api/auth/verify", json={"phone": SPEC_PHONE, "code": "111111" if code != "111111" else "222222"})
        r = await c.post("/api/auth/verify", json={"phone": SPEC_PHONE, "code": code})
        assert r.status_code == 401  # после 5 ошибок даже верный код не подходит


@pytest.mark.parametrize("staff_chat_id", [-888])
async def test_login_requires_staff_chat(web):
    web.bot.staff_members.discard(SPEC_MAX)
    async with client() as c:
        await c.post("/api/auth/request", json={"phone": SPEC_PHONE})
    assert web.bot.to(SPEC_MAX) == []  # не сотрудник — код не отправляем


# --- права ---


async def test_viewer_is_read_only(web):
    async with await login(web, VIEWER_PHONE, VIEWER_MAX) as c:
        assert (await c.get("/api/stats")).status_code == 200
        items = (await c.get("/api/requests")).json()["items"]
        assert items and "applicant" not in items[0]  # руководству персональные данные не нужны
        assert "applicant_url" not in items[0]
        assert (await c.post("/api/requests/1/action", json={"action": "take"})).status_code == 403
        assert (await c.get("/api/staff")).status_code == 403


async def test_specialist_works_requests(web):
    async with await login(web, SPEC_PHONE, SPEC_MAX) as c:
        items = (await c.get("/api/requests")).json()["items"]
        assert items[0]["applicant"] == "Иван Заявитель"
        assert items[0]["applicant_url"] == f"max://user/{APPLICANT_MAX}"

        r = await c.post("/api/requests/1/action", json={"action": "take"})
        assert r.status_code == 200 and r.json()["assignee"] == "Пётр Сетевик"
        assert web.bot.to(APPLICANT_MAX)[-1] == web.org.text("notify_in_progress", id=1)
        assert [i["id"] for i in (await c.get("/api/requests?scope=mine")).json()["items"]] == [1]

        # Уже в работе — второй раз взять нельзя
        assert (await c.post("/api/requests/1/action", json={"action": "take"})).status_code == 409

        r = await c.post("/api/requests/1/category", json={"category": "computers"})
        assert r.json()["category"] == "computers"

        assert (await c.post("/api/requests/1/assign", json={"assignee_id": 1})).status_code == 403
        assert (await c.patch("/api/requests/1", json={"room": "1"})).status_code == 403
        assert (await c.get("/api/staff")).status_code == 403

        r = await c.post("/api/requests/1/action", json={"action": "done"})
        assert r.json()["status"] == "done"
        assert [i["id"] for i in (await c.get("/api/requests?scope=closed")).json()["items"]] == [1]


async def test_admin_assigns_and_edits(web):
    async with await login(web, ADMIN_PHONE, ADMIN_MAX) as c:
        spec_id = next(a["id"] for a in (await c.get("/api/meta")).json()["assignees"] if a["name"] == "Пётр Сетевик")
        r = await c.post("/api/requests/1/assign", json={"assignee_id": spec_id})
        assert r.status_code == 200 and r.json()["status"] == "in_progress" and r.json()["assignee"] == "Пётр Сетевик"
        assert "Вам назначена заявка №1." in web.bot.to(SPEC_MAX)

        r = await c.patch("/api/requests/1", json={"building": "2", "room": "301", "description": "Нет интернета у окна"})
        assert (r.json()["building"], r.json()["room"]) == ("2", "301")
        assert (await c.patch("/api/requests/1", json={"building": "нет"})).status_code == 409


async def test_admin_manages_staff(web):
    async with await login(web, ADMIN_PHONE, ADMIN_MAX) as admin:
        r = await admin.post("/api/staff", json={"name": "Новый Электрик", "phone": "89005556677",
                                                  "role": "specialist", "directions": ["electric"]})
        assert r.status_code == 200 and r.json()["phone"] == "79005556677" and not r.json()["linked"]
        new_id = r.json()["id"]
        dup = await admin.post("/api/staff", json={"name": "Дубль", "phone": "+7 900 555-66-77"})
        assert dup.status_code == 409
        assert (await admin.post("/api/staff", json={"name": "X", "phone": "+79001110000",
                                                     "directions": ["nosuch"]})).status_code == 422

        r = await admin.patch(f"/api/staff/{new_id}", json={"directions": ["electric", "plumbing"], "role": "admin"})
        assert r.json()["directions"] == ["electric", "plumbing"] and r.json()["role"] == "admin"

        # Удаление сотрудника без истории — удаляется совсем
        assert (await admin.delete(f"/api/staff/{new_id}")).json() == {"deleted": True, "archived": False}

        # Нельзя разжаловать или отключить себя
        me = (await admin.get("/api/me")).json()["id"]
        assert (await admin.patch(f"/api/staff/{me}", json={"role": "viewer"})).status_code == 409
        assert (await admin.patch(f"/api/staff/{me}", json={"active": False})).status_code == 409
        assert (await admin.delete(f"/api/staff/{me}")).status_code == 409


async def test_phone_change_or_deactivation_logs_user_out(web):
    spec = await login(web, SPEC_PHONE, SPEC_MAX)
    async with await login(web, ADMIN_PHONE, ADMIN_MAX) as admin:
        spec_id = (await spec.get("/api/me")).json()["id"]
        assert (await spec.get("/api/me")).status_code == 200

        r = await admin.patch(f"/api/staff/{spec_id}", json={"phone": "+79008887766"})
        assert r.json()["linked"] is False  # новый номер нужно заново подтвердить в боте
        assert (await spec.get("/api/me")).status_code == 401
    await spec.aclose()


async def test_delete_with_history_archives(web):
    async with await login(web, SPEC_PHONE, SPEC_MAX) as spec:
        await spec.post("/api/requests/1/action", json={"action": "take"})
        spec_id = (await spec.get("/api/me")).json()["id"]
        async with await login(web, ADMIN_PHONE, ADMIN_MAX) as admin:
            assert (await admin.delete(f"/api/staff/{spec_id}")).json() == {"deleted": False, "archived": True}
            row = next(a for a in (await admin.get("/api/staff")).json()["items"] if a["id"] == spec_id)
            assert row["active"] is False
        assert (await spec.get("/api/me")).status_code == 401
    # История заявки сохранилась
    async with web.db.session() as s:
        req = await repo.get_request(s, web.org.org_id, 1)
    assert req.assignee.name == "Пётр Сетевик" and req.status == Status.IN_PROGRESS


# --- защита от подделки запросов ---


async def test_csrf_guards(web):
    async with await login(web, SPEC_PHONE, SPEC_MAX) as c:
        r = await c.post("/api/requests/1/action", content="action=take",
                         headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415
        r = await c.post("/api/requests/1/action", json={"action": "take"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403


async def test_demo_db_is_open_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.delenv("STAFF_CHAT_ID", raising=False)
    async with app.router.lifespan_context(app):
        async with client() as c:
            assert (await c.get("/")).status_code == 200
            me = (await c.get("/api/me")).json()
            assert me["role"] == "viewer" and me["demo"] is True
            assert (await c.get("/api/stats")).status_code == 200
            assert (await c.post("/api/requests/1/action", json={"action": "take"})).status_code == 403


# --- что видит специалист ---


async def test_specialist_visibility(web):
    org = web.org
    async with web.db.session() as s, s.begin():
        # №2 — закрыта, чужое направление (мебель); №3 — закрыта, его направление (сеть)
        for cat in ("furniture", "network"):
            r = await repo.create_request(s, org_id=org.org_id, building="1", room="1", description="Закрытая заявка",
                                          category=cat, applicant_max_id=7, applicant_name="Х")
            await repo.set_status(s, r, Status.IN_PROGRESS, changed_by=1)
            await repo.set_status(s, r, Status.DONE, changed_by=1)
        # №4 — открыта, чужое направление: открытые видны все
        await repo.create_request(s, org_id=org.org_id, building="1", room="1", description="Сломан стул",
                                  category="furniture", applicant_max_id=7, applicant_name="Х")

    async with await login(web, SPEC_PHONE, SPEC_MAX) as c:
        assert (await c.get("/api/stats")).status_code == 403  # аналитика — не для специалиста
        assert "analytics" not in (await c.get("/api/me")).json()["perms"]
        active = {i["id"] for i in (await c.get("/api/requests?scope=active")).json()["items"]}
        assert active == {1, 4}
        closed = {i["id"] for i in (await c.get("/api/requests?scope=closed")).json()["items"]}
        assert closed == {3}
        direction = {i["id"] for i in (await c.get("/api/requests?scope=direction")).json()["items"]}
        assert direction == {1}
        assert (await c.get("/api/requests/2/history")).status_code == 404
        assert (await c.get("/api/requests/3/history")).status_code == 200

    async with await login(web, VIEWER_PHONE, VIEWER_MAX) as c:  # руководство видит всё
        assert {i["id"] for i in (await c.get("/api/requests?scope=closed")).json()["items"]} == {2, 3}
        assert (await c.get("/api/stats")).status_code == 200
