"""Корпуса: управление в дашборде, бот показывает только активные и в заданном порядке."""

from __future__ import annotations

from app import buildings, repo
from tests.test_dispatcher import disp  # noqa: F401 — фикстура
from tests.test_web import client, login, staff_chat_id, web  # noqa: F401 — фикстуры


async def test_buildings_crud(web, disp):  # noqa: F811
    items = (await disp.get("/api/buildings")).json()["items"]
    assert [b["title"] for b in items] == ["Корпус 1", "Корпус 2", "Корпус 3", "Общежитие"]

    r = await disp.post("/api/buildings", json={"title": "Мастерские"})
    assert r.status_code == 200
    code = r.json()["code"]
    assert (await disp.post("/api/buildings", json={"title": "мастерские"})).status_code == 409
    assert (await disp.post("/api/buildings", json={"title": "x" * 41})).status_code == 422

    meta = (await disp.get("/api/meta")).json()
    assert {"code": code, "title": "Мастерские", "active": True} in meta["buildings"]

    r = await disp.patch(f"/api/buildings/{code}", json={"title": "Учебные мастерские"})
    assert r.json()["title"] == "Учебные мастерские"
    assert (await disp.delete(f"/api/buildings/{code}")).json() == {"deleted": True, "archived": False}
    # В корпусе 1 есть заявка (из фикстуры web) — удаление только отключает его
    assert (await disp.delete("/api/buildings/1")).json() == {"deleted": False, "archived": True}
    assert web.org.building_titles["1"] == "Корпус 1"
    assert "1" not in {b.code for b in web.org.active_buildings}


async def test_last_active_building_stays(web, disp):  # noqa: F811
    for code in ("1", "2", "3"):
        assert (await disp.patch(f"/api/buildings/{code}", json={"active": False})).status_code == 200
    assert (await disp.patch("/api/buildings/dorm", json={"active": False})).status_code == 409
    assert (await disp.delete("/api/buildings/dorm")).status_code == 409


async def test_move_changes_order(web, disp):  # noqa: F811
    await disp.patch("/api/buildings/dorm", json={"move": -1})
    await disp.patch("/api/buildings/1", json={"move": -1})  # уже первый — ничего не меняется
    items = (await disp.get("/api/buildings")).json()["items"]
    assert [b["code"] for b in items] == ["1", "2", "dorm", "3"]


async def test_inactive_building_rejected_in_edit(web, disp):  # noqa: F811
    await disp.patch("/api/buildings/3", json={"active": False})
    r = await disp.patch("/api/requests/1", json={"building": "3", "room": "1", "description": "Не работает проектор"})
    assert r.status_code in (400, 409, 422)


async def test_specialist_cannot_manage_buildings(web):  # noqa: F811
    async with await login(web, "+79000000002", 1002) as spec:
        assert (await spec.get("/api/buildings")).status_code == 403
        assert (await spec.post("/api/buildings", json={"title": "Склад"})).status_code == 403


async def test_bot_shows_only_active_buildings(h, db, org):
    async with db.session() as s, s.begin():
        await buildings.update(s, org.org_id, "2", {"active": False})
        await buildings.create(s, org.org_id, title="Мастерские")
        await buildings.move(s, org.org_id, "dorm", -1)
    await buildings.refresh(db, org, force=True)

    await h.press("a|new")
    assert h.last_buttons()[:4] == ["b|1", "b|dorm", "b|3", "b|b5"]  # «Корпус 2» отключён

    # Старая кнопка отключённого корпуса не принимается
    await h.press("b|2")
    await h.text("214")
    async with db.session() as s:
        assert await repo.get_request(s, org.org_id, 1) is None


async def test_config_seeds_buildings_once(db, org):
    async with db.session() as s, s.begin():
        await buildings.update(s, org.org_id, "1", {"title": "Главный корпус"})
        await buildings.seed_from_config(s, org)
    await buildings.refresh(db, org, force=True)
    assert org.building_titles["1"] == "Главный корпус"
