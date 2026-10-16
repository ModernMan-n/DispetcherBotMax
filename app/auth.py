"""Вход в веб-интерфейс по одноразовому коду из бота MAX и права ролей.

Паролей нет: сотрудник вводит номер телефона, бот присылает код в личку MAX. Войти может
только тот, кто уже подтвердил номер в боте (/staff), — так личность проверяет сам MAX.
Коды и токены сессий храним только в виде хешей.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import staff
from app.models import Assignee, LoginCode, Role, WebSession, utcnow

CODE_TTL = timedelta(minutes=5)
CODE_COOLDOWN = timedelta(seconds=60)  # не чаще одного кода в минуту на человека
CODE_MAX_ATTEMPTS = 5
SESSION_TTL = timedelta(days=7)


class Perm(StrEnum):
    VIEW = "view"  # список заявок (специалисту — только видимые ему, см. VIEW_ALL)
    ANALYTICS = "analytics"  # «Обзор»: показатели, исполнители, просроченные
    VIEW_ALL = "view_all"  # все заявки, включая чужие закрытые
    WORK = "work"  # взять, закрыть, вернуть, отменить, сменить категорию
    ADMIN_REQUESTS = "admin_requests"  # назначить, править заявку, срок, приоритет
    MANAGE_CATEGORIES = "manage_categories"  # категории: добавить, изменить, отключить
    MANAGE_BUILDINGS = "manage_buildings"  # корпуса: добавить, переименовать, порядок, отключить
    MANAGE_STAFF = "manage_staff"  # сотрудники: добавить, изменить, удалить


ROLE_PERMS: dict[Role, frozenset[Perm]] = {
    Role.VIEWER: frozenset({Perm.VIEW, Perm.ANALYTICS, Perm.VIEW_ALL}),
    # Специалист: все открытые заявки, а закрытые — только своих направлений или взятые им
    Role.SPECIALIST: frozenset({Perm.VIEW, Perm.WORK}),
    Role.DISPATCHER: frozenset({
        Perm.VIEW, Perm.ANALYTICS, Perm.VIEW_ALL, Perm.WORK, Perm.ADMIN_REQUESTS, Perm.MANAGE_CATEGORIES,
        Perm.MANAGE_BUILDINGS,
    }),
    Role.ADMIN: frozenset(Perm),
}

ROLE_TITLES = {
    Role.ADMIN: "Администратор",
    Role.DISPATCHER: "Диспетчер",
    Role.SPECIALIST: "Специалист",
    Role.VIEWER: "Руководство",
}


def perms_of(a: Assignee) -> frozenset[Perm]:
    return ROLE_PERMS.get(Role(a.role), frozenset())


class AuthError(Exception):
    """Сообщение для пользователя — в тексте исключения."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _find_linked(s: AsyncSession, org_id: str, phone: str) -> Assignee | None:
    norm = staff.normalize_phone(phone)
    if norm is None:
        return None
    return await s.scalar(
        select(Assignee).where(
            Assignee.org_id == org_id,
            Assignee.phone == norm,
            Assignee.active.is_(True),
            Assignee.max_id.is_not(None),
        )
    )


async def request_code(
    s: AsyncSession, org_id: str, phone: str, now: datetime | None = None
) -> tuple[Assignee, str] | None:
    """Готовит код для входа. None — номер не подходит (наружу об этом не сообщаем)."""
    now = now or utcnow()
    a = await _find_linked(s, org_id, phone)
    if a is None:
        return None
    last = await s.scalar(
        select(LoginCode).where(LoginCode.assignee_id == a.id).order_by(LoginCode.created_at.desc()).limit(1)
    )
    if last is not None and now - last.created_at < CODE_COOLDOWN:
        raise AuthError("Код уже отправлен. Новый можно запросить через минуту.")
    # Старые коды больше не действуют
    await s.execute(delete(LoginCode).where(LoginCode.assignee_id == a.id))
    code = f"{secrets.randbelow(1_000_000):06d}"
    s.add(LoginCode(assignee_id=a.id, code_hash=_hash(code), created_at=now, expires_at=now + CODE_TTL))
    await s.flush()
    return a, code


async def verify_code(s: AsyncSession, org_id: str, phone: str, code: str, now: datetime | None = None) -> Assignee:
    now = now or utcnow()
    wrong = AuthError("Неверный или просроченный код.")
    a = await _find_linked(s, org_id, phone)
    if a is None:
        raise wrong
    row = await s.scalar(
        select(LoginCode).where(LoginCode.assignee_id == a.id, LoginCode.used.is_(False))
        .order_by(LoginCode.created_at.desc()).limit(1)
    )
    if row is None or row.expires_at < now or row.attempts >= CODE_MAX_ATTEMPTS:
        raise wrong
    row.attempts += 1
    if not secrets.compare_digest(row.code_hash, _hash(code.strip())):
        if row.attempts >= CODE_MAX_ATTEMPTS:
            raise AuthError("Слишком много попыток. Запросите новый код.")
        raise wrong
    row.used = True
    return a


async def create_session(s: AsyncSession, assignee_id: int, now: datetime | None = None) -> str:
    now = now or utcnow()
    token = secrets.token_urlsafe(32)
    s.add(WebSession(token_hash=_hash(token), assignee_id=assignee_id, created_at=now, expires_at=now + SESSION_TTL))
    await s.flush()
    return token


async def session_user(s: AsyncSession, org_id: str, token: str | None, now: datetime | None = None) -> Assignee | None:
    """Сотрудник по токену сессии. Отключённый, отвязанный или с чужой организации — None."""
    if not token:
        return None
    now = now or utcnow()
    sess = await s.get(WebSession, _hash(token))
    if sess is None or sess.expires_at < now:
        return None
    a = sess.assignee
    if a is None or a.org_id != org_id or not a.active or a.max_id is None or a.phone is None:
        return None
    return a


async def end_session(s: AsyncSession, token: str | None) -> None:
    if token:
        await s.execute(delete(WebSession).where(WebSession.token_hash == _hash(token)))


async def end_all_sessions(s: AsyncSession, assignee_id: int) -> None:
    """Выкинуть сотрудника отовсюду: отключили, сменили номер или роль понизили."""
    await s.execute(delete(WebSession).where(WebSession.assignee_id == assignee_id))
    await s.execute(delete(LoginCode).where(LoginCode.assignee_id == assignee_id))
