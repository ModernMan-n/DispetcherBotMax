"""Операции с заявками. Вся бизнес-логика статусов живёт здесь, а не в хендлерах бота."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_STATUSES, TRANSITIONS, Assignee, Request, Status, StatusLog, utcnow


class TransitionError(Exception):
    pass


async def create_request(
    s: AsyncSession,
    *,
    org_id: str,
    building: str,
    room: str,
    description: str,
    category: str,
    applicant_max_id: int,
    applicant_name: str,
    now: datetime | None = None,
) -> Request:
    now = now or utcnow()
    req = Request(
        org_id=org_id,
        created_at=now,
        building=building,
        room=room,
        description=description,
        category=category,
        status=Status.NEW,
        applicant_max_id=applicant_max_id,
        applicant_name=applicant_name,
        log=[StatusLog(status=Status.NEW, changed_at=now, changed_by=applicant_max_id)],
    )
    s.add(req)
    await s.flush()
    return req


async def set_status(
    s: AsyncSession,
    req: Request,
    status: Status,
    *,
    changed_by: int | None,
    assignee_id: int | None = None,
    now: datetime | None = None,
) -> Request:
    current = Status(req.status)
    if status not in TRANSITIONS[current]:
        raise TransitionError(f"Нельзя перевести заявку №{req.id} из {current} в {status}")
    now = now or utcnow()

    if status == Status.IN_PROGRESS:
        req.taken_at = now
        if assignee_id is not None:
            req.assignee_id = assignee_id
    elif status == Status.NEW:
        # Заявку вернули в очередь: сбрасываем исполнителя.
        req.taken_at = None
        req.assignee_id = None
    elif status in (Status.DONE, Status.CANCELLED):
        req.closed_at = now

    req.status = status
    req.log.append(StatusLog(status=status, changed_at=now, changed_by=changed_by))
    await s.flush()
    return req


async def get_request(s: AsyncSession, org_id: str, request_id: int) -> Request | None:
    return await s.scalar(select(Request).where(Request.org_id == org_id, Request.id == request_id))


async def list_active_for_applicant(
    s: AsyncSession, org_id: str, applicant_max_id: int, limit: int = 10
) -> list[Request]:
    rows = await s.scalars(
        select(Request)
        .where(
            Request.org_id == org_id,
            Request.applicant_max_id == applicant_max_id,
            Request.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Request.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def set_category(s: AsyncSession, req: Request, category: str) -> Request:
    """Ручная правка категории диспетчером."""
    req.category = category
    req.category_source = "manual"
    await s.flush()
    return req


async def get_or_create_assignee(s: AsyncSession, org_id: str, max_id: int, name: str) -> Assignee:
    """Исполнитель — тот, кто нажал «Взять». Заводится автоматически при первом нажатии."""
    assignee = await s.scalar(select(Assignee).where(Assignee.org_id == org_id, Assignee.max_id == max_id))
    if assignee is None:
        assignee = Assignee(org_id=org_id, max_id=max_id, name=name, active=True)
        s.add(assignee)
        await s.flush()
    return assignee
