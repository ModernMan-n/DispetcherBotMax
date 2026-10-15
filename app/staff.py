"""Специалисты: учёт, вход по номеру телефона, линия, выборки заявок по направлениям."""

from __future__ import annotations

import re

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ACTIVE_STATUSES,
    PRIORITY_RANK,
    WORKING_ROLES,
    Assignee,
    AssigneeDirection,
    CardMessage,
    Request,
    Role,
)


class StaffError(Exception):
    pass


def normalize_phone(raw: str) -> str | None:
    """Приводит российский номер к виду 7XXXXXXXXXX. Не похоже на номер — None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "7" + digits
    return None


def mask_phone(phone: str | None) -> str:
    """Для логов и списков: +7 *** ***-12-34."""
    if not phone:
        return "—"
    return f"+7 *** ***-{phone[-4:-2]}-{phone[-2:]}"


# --- учёт (пока из командной строки, потом — из админки дашборда) ---


async def add_specialist(
    s: AsyncSession, org_id: str, *, name: str, phone: str, directions: list[str], role: Role = Role.SPECIALIST
) -> Assignee:
    norm = normalize_phone(phone)
    if norm is None:
        raise StaffError(f"Не похоже на номер телефона: {phone!r}")
    if await s.scalar(select(Assignee).where(Assignee.org_id == org_id, Assignee.phone == norm)):
        raise StaffError("Специалист с таким номером уже есть")
    a = Assignee(
        org_id=org_id, name=name, phone=norm, active=True, on_duty=False, role=role,
        directions=[AssigneeDirection(category=c) for c in dict.fromkeys(directions)],
    )
    s.add(a)
    await s.flush()
    return a


async def set_directions(s: AsyncSession, a: Assignee, directions: list[str]) -> None:
    a.directions = [AssigneeDirection(category=c) for c in dict.fromkeys(directions)]
    await s.flush()


async def list_specialists(s: AsyncSession, org_id: str) -> list[Assignee]:
    rows = await s.scalars(
        select(Assignee).where(Assignee.org_id == org_id, Assignee.phone.is_not(None)).order_by(Assignee.name)
    )
    return list(rows)


async def get_specialist_by_id(s: AsyncSession, org_id: str, assignee_id: int) -> Assignee | None:
    return await s.scalar(
        select(Assignee).where(
            Assignee.org_id == org_id, Assignee.id == assignee_id, Assignee.phone.is_not(None)
        )
    )


# --- вход в боте ---


async def get_staff_member(s: AsyncSession, org_id: str, max_id: int) -> Assignee | None:
    """Активный сотрудник любой роли, привязанный к этому аккаунту MAX."""
    return await s.scalar(
        select(Assignee).where(
            Assignee.org_id == org_id,
            Assignee.max_id == max_id,
            Assignee.phone.is_not(None),
            Assignee.active.is_(True),
        )
    )


async def get_specialist(s: AsyncSession, org_id: str, max_id: int) -> Assignee | None:
    """Сотрудник, который работает с заявками (специалист или администратор)."""
    a = await get_staff_member(s, org_id, max_id)
    return a if a is not None and a.works_requests else None


async def link_by_phone(s: AsyncSession, org_id: str, phone: str, max_id: int) -> Assignee:
    """Привязывает аккаунт MAX к специалисту с этим номером. Номер должен быть подтверждён MAX."""
    norm = normalize_phone(phone)
    a = None if norm is None else await s.scalar(
        select(Assignee).where(Assignee.org_id == org_id, Assignee.phone == norm, Assignee.active.is_(True))
    )
    if a is None:
        raise StaffError("not_found")
    if a.max_id is not None and a.max_id != max_id:
        raise StaffError("linked_to_other")
    # Если этот аккаунт раньше «взял» заявку в чате без регистрации — запись останется отдельной,
    # история не теряется.
    a.max_id = max_id
    a.on_duty = a.works_requests  # руководству заявки не рассылаются
    await s.flush()
    return a


# --- заявки и уведомления ---


async def list_active_requests(
    s: AsyncSession, org_id: str, categories: list[str] | None = None, limit: int = 20
) -> list[Request]:
    q = select(Request).where(Request.org_id == org_id, Request.status.in_(ACTIVE_STATUSES))
    if categories is not None:
        q = q.where(Request.category.in_(categories))
    return list(await s.scalars(q.order_by(*priority_order()).limit(limit)))


def priority_order():
    """Срочные первыми, дальше — у кого срок раньше."""
    rank = case({p.value: r for p, r in PRIORITY_RANK.items()}, value=Request.priority, else_=2)
    return rank, Request.due_at.is_(None), Request.due_at, Request.created_at


async def on_duty_for_category(s: AsyncSession, org_id: str, category: str) -> list[Assignee]:
    rows = await s.scalars(
        select(Assignee)
        .join(AssigneeDirection)
        .where(
            Assignee.org_id == org_id,
            Assignee.active.is_(True),
            Assignee.on_duty.is_(True),
            Assignee.max_id.is_not(None),
            Assignee.role.in_(WORKING_ROLES),
            AssigneeDirection.category == category,
        )
    )
    return list(rows)


async def save_card(
    s: AsyncSession, request_id: int, message_id: str, *, chat_id: int | None = None, user_id: int | None = None
) -> None:
    s.add(CardMessage(request_id=request_id, message_id=message_id, chat_id=chat_id, user_id=user_id))
    await s.flush()


async def card_messages(s: AsyncSession, request_id: int) -> list[CardMessage]:
    return list(await s.scalars(select(CardMessage).where(CardMessage.request_id == request_id)))
