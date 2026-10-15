"""Операции с заявками. Вся бизнес-логика статусов живёт здесь, а не в хендлерах бота."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ACTIVE_STATUSES,
    TRANSITIONS,
    Assignee,
    Priority,
    Request,
    RequestAssignee,
    RequestEvent,
    Status,
    StatusLog,
    utcnow,
)


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
    sla_hours: int | None = None,
) -> Request:
    """sla_hours — норматив категории: из него срок выполнения (если не задан — срока нет)."""
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
        priority=Priority.NORMAL,
        due_at=now + timedelta(hours=sla_hours) if sla_hours is not None else None,
        log=[StatusLog(status=Status.NEW, changed_at=now, changed_by=applicant_max_id)],
    )
    s.add(req)
    await s.flush()
    await s.refresh(req, ["team", "assignee"])  # состав (пока пустой) нужен карточке уже после сессии
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
    # Бот и веб-интерфейс — разные процессы. Меняем статус, только если он всё ещё тот,
    # что мы прочитали: иначе двое могли бы взять одну заявку.
    result = await s.execute(
        update(Request)
        .where(Request.id == req.id, Request.status == current)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await s.refresh(req)
        raise TransitionError(f"Заявку №{req.id} уже изменили")

    if status == Status.IN_PROGRESS:
        req.taken_at = now
        if assignee_id is not None:
            await set_team(s, req, [assignee_id], now=now)
    elif status == Status.NEW:
        # Заявку вернули в очередь: сбрасываем всех исполнителей.
        req.taken_at = None
        await set_team(s, req, [], now=now)
    elif status in (Status.DONE, Status.CANCELLED):
        req.closed_at = now

    req.status = status
    req.log.append(StatusLog(status=status, changed_at=now, changed_by=changed_by))
    await s.flush()
    return req


async def set_team(s: AsyncSession, req: Request, assignee_ids: list[int], *, now: datetime | None = None) -> None:
    """Состав исполнителей заявки. Оставшиеся сохраняют порядок, новые добавляются в конец.

    req.assignee_id — первый исполнитель (основной), чтобы старый код и аналитика работали как раньше.
    """
    now = now or utcnow()
    wanted = list(dict.fromkeys(assignee_ids))
    rows = list(await s.scalars(
        select(RequestAssignee).where(RequestAssignee.request_id == req.id).order_by(RequestAssignee.added_at)
    ))
    for r in rows:
        if r.assignee_id not in wanted:
            await s.delete(r)
    have = {r.assignee_id for r in rows}
    for offset, aid in enumerate(a for a in wanted if a not in have):
        # Микросекундный сдвиг — чтобы порядок добавления сохранялся
        s.add(RequestAssignee(request_id=req.id, assignee_id=aid, added_at=now + timedelta(microseconds=offset)))
    await s.flush()
    order = [r.assignee_id for r in rows if r.assignee_id in wanted] + [a for a in wanted if a not in have]
    req.assignee_id = order[0] if order else None
    await s.flush()
    await s.refresh(req, ["team", "assignee"])


def team_ids(req: Request) -> list[int]:
    return [a.id for a in req.team]


async def leave_team(s: AsyncSession, req: Request, assignee: Assignee) -> bool:
    """«Вернуть в очередь» от одного из нескольких исполнителей: снимаем только его.

    True — снят, заявка остаётся в работе у остальных; False — он единственный (или не в команде),
    тогда заявку целиком возвращают в очередь обычной сменой статуса.
    """
    ids = team_ids(req)
    if assignee.id not in ids or len(ids) < 2:
        return False
    await set_team(s, req, [i for i in ids if i != assignee.id])
    await log_event(s, req, "assign", f"Снял себя с заявки: {assignee.name}", actor_id=assignee.id, actor_name=assignee.name)
    return True


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


async def set_category(s: AsyncSession, req: Request, category: str, sla_hours: int | None = None) -> Request:
    """Ручная правка категории. Срок пересчитывается по новому нормативу, если его не задавали вручную."""
    req.category = category
    req.category_source = "manual"
    if sla_hours is not None and not req.due_manual:
        req.due_at = req.created_at + timedelta(hours=sla_hours)
    await s.flush()
    return req


async def log_event(
    s: AsyncSession, req: Request, kind: str, text: str, *, actor_id: int | None, actor_name: str | None
) -> None:
    """Запись в историю заявки (срок, приоритет, правка, категория, назначение)."""
    s.add(RequestEvent(request_id=req.id, kind=kind, text=text, actor_id=actor_id or None, actor_name=actor_name))
    await s.flush()


async def request_events(s: AsyncSession, request_id: int) -> list[RequestEvent]:
    return list(await s.scalars(
        select(RequestEvent).where(RequestEvent.request_id == request_id).order_by(RequestEvent.created_at)
    ))


async def get_or_create_assignee(s: AsyncSession, org_id: str, max_id: int, name: str) -> Assignee:
    """Исполнитель — тот, кто нажал «Взять». Заводится автоматически при первом нажатии."""
    assignee = await s.scalar(select(Assignee).where(Assignee.org_id == org_id, Assignee.max_id == max_id))
    if assignee is None:
        assignee = Assignee(org_id=org_id, max_id=max_id, name=name, active=True)
        s.add(assignee)
        await s.flush()
    return assignee
