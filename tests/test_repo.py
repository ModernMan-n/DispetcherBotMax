from datetime import UTC, datetime, timedelta

import pytest

from app import repo
from app.models import Assignee, Status


async def _create(s, org, user_id=1, now=None):
    return await repo.create_request(
        s,
        org_id=org.org_id,
        building="1",
        room="214",
        description="Не работает розетка",
        category="electric",
        applicant_max_id=user_id,
        applicant_name="Тест",
        now=now,
    )


async def test_full_lifecycle(db, org):
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    async with db.session() as s, s.begin():
        worker = Assignee(org_id=org.org_id, name="Электрик", max_id=42)
        s.add(worker)
        await s.flush()
        req = await _create(s, org, now=t0)
        await repo.set_status(
            s, req, Status.IN_PROGRESS, changed_by=42, assignee_id=worker.id, now=t0 + timedelta(minutes=15)
        )
        await repo.set_status(s, req, Status.DONE, changed_by=42, now=t0 + timedelta(hours=2))
        req_id = req.id

    async with db.session() as s:
        req = await repo.get_request(s, org.org_id, req_id)
        assert req.status == Status.DONE
        assert req.assignee.name == "Электрик"
        assert req.taken_at - req.created_at == timedelta(minutes=15)
        assert req.closed_at - req.created_at == timedelta(hours=2)
        assert req.closed_at.tzinfo is not None
        assert [entry.status for entry in req.log] == ["new", "in_progress", "done"]


async def test_invalid_transition(db, org):
    async with db.session() as s, s.begin():
        req = await _create(s, org)
        with pytest.raises(repo.TransitionError):
            await repo.set_status(s, req, Status.DONE, changed_by=1)


async def test_return_to_queue_resets_assignee(db, org):
    async with db.session() as s, s.begin():
        worker = Assignee(org_id=org.org_id, name="Сисадмин", max_id=7)
        s.add(worker)
        await s.flush()
        req = await _create(s, org)
        await repo.set_status(s, req, Status.IN_PROGRESS, changed_by=7, assignee_id=worker.id)
        await repo.set_status(s, req, Status.NEW, changed_by=7)
        assert req.assignee_id is None and req.taken_at is None


async def test_active_list_is_per_user_and_org(db, org):
    async with db.session() as s, s.begin():
        await _create(s, org, user_id=1)
        done = await _create(s, org, user_id=1)
        await repo.set_status(s, done, Status.CANCELLED, changed_by=1)
        await _create(s, org, user_id=2)

    async with db.session() as s:
        mine = await repo.list_active_for_applicant(s, org.org_id, 1)
        assert len(mine) == 1
        assert await repo.list_active_for_applicant(s, "other-org", 1) == []
