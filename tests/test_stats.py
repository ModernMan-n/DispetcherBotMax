from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from app import repo, stats
from app.models import Assignee, Status

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


async def _req(s, org, *, created, category="electric", building="1"):
    return await repo.create_request(
        s, org_id=org.org_id, building=building, room="101", description="тест",
        category=category, applicant_max_id=1, applicant_name="Заявитель", now=created,
    )


async def test_metrics(db, org):
    # electric: норматив 8 ч
    async with db.session() as s, s.begin():
        w = Assignee(org_id=org.org_id, name="Электрик", max_id=42)
        s.add(w)
        await s.flush()

        # закрыта в срок: взята через 10 мин, закрыта через 2 ч
        r1 = await _req(s, org, created=NOW - timedelta(days=2))
        await repo.set_status(s, r1, Status.IN_PROGRESS, changed_by=42, assignee_id=w.id,
                              now=r1.created_at + timedelta(minutes=10))
        await repo.set_status(s, r1, Status.DONE, changed_by=42, now=r1.created_at + timedelta(hours=2))

        # закрыта с опозданием: взята через 30 мин, закрыта через 10 ч
        r2 = await _req(s, org, created=NOW - timedelta(days=3), building="2")
        await repo.set_status(s, r2, Status.IN_PROGRESS, changed_by=42, assignee_id=w.id,
                              now=r2.created_at + timedelta(minutes=30))
        await repo.set_status(s, r2, Status.DONE, changed_by=42, now=r2.created_at + timedelta(hours=10))

        # активная и просроченная (висит 9 ч при нормативе 8)
        await _req(s, org, created=NOW - timedelta(hours=9))
        # активная, не просроченная (мебель — 72 ч)
        await _req(s, org, created=NOW - timedelta(hours=9), category="furniture")
        # старая, вне периода 7 дней
        old = await _req(s, org, created=NOW - timedelta(days=40))
        await repo.set_status(s, old, Status.CANCELLED, changed_by=1, now=old.created_at + timedelta(hours=1))

    async with db.session() as s:
        d = await stats.collect(s, org, days=7, now=NOW)

    assert d["now"] == {"new": 2, "in_progress": 0, "overdue": 1}
    p = d["period"]
    assert (p["created"], p["done"], p["cancelled"]) == (4, 2, 0)
    assert p["avg_take_min"] == 20.0
    # Время выполнения — от взятия до «Готово»: 1 ч 50 мин и 9 ч 30 мин → в среднем 5 ч 40 мин
    assert p["avg_close_min"] == 340.0 and p["avg_close"] == "5 ч 40 мин"
    assert p["on_time_pct"] == 50

    [a] = d["assignees"]
    assert (a["name"], a["done"], a["on_time_pct"]) == ("Электрик", 2, 50)

    [od] = d["overdue"]
    assert od["over_by"] == "1 ч" and od["category"] == "Электрика"
    # на дашборд не уходят данные заявителя
    assert "Заявитель" not in str(d)


async def test_empty_db(db, org):
    async with db.session() as s:
        d = await stats.collect(s, org, days=30, now=NOW)
    assert d["period"]["avg_close"] is None and d["period"]["on_time_pct"] is None
    assert d["overdue"] == [] and d["assignees"] == []


async def test_api(monkeypatch, tmp_path):
    from app.dashboard.web import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page = await client.get("/")
            assert page.status_code == 200 and "Дашборд" in page.text
            res = await client.get("/api/stats", params={"days": 7})
            assert res.status_code == 200
            assert res.json()["demo"] is True
            assert (await client.get("/api/stats", params={"days": 0})).status_code == 422


async def test_change_vs_previous_period(db, org):
    async with db.session() as s, s.begin():
        # Текущие 7 дней: 2 заявки, одна закрыта за 2 ч
        cur = await _req(s, org, created=NOW - timedelta(days=1))
        await repo.set_status(s, cur, Status.IN_PROGRESS, changed_by=1, now=cur.created_at + timedelta(minutes=10))
        await repo.set_status(s, cur, Status.DONE, changed_by=1, now=cur.created_at + timedelta(hours=2))
        await _req(s, org, created=NOW - timedelta(days=2))
        # Предыдущие 7 дней: 1 заявка, закрыта за 4 ч
        old = await _req(s, org, created=NOW - timedelta(days=10))
        await repo.set_status(s, old, Status.IN_PROGRESS, changed_by=1, now=old.created_at + timedelta(minutes=20))
        await repo.set_status(s, old, Status.DONE, changed_by=1, now=old.created_at + timedelta(hours=4))
        # Ещё раньше — не учитывается ни там, ни там
        await _req(s, org, created=NOW - timedelta(days=20))

    async with db.session() as s:
        d = await stats.collect(s, org, days=7, now=NOW)
    assert d["change"] == {"created": 100.0, "done": 0.0, "avg_take": -50.0, "avg_close": -50.0}


async def test_change_none_without_previous_data(db, org):
    async with db.session() as s, s.begin():
        await _req(s, org, created=NOW - timedelta(days=1))
    async with db.session() as s:
        d = await stats.collect(s, org, days=7, now=NOW)
    assert d["change"]["created"] is None
