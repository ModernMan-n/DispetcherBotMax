"""Корпуса организации — в БД, ими управляет диспетчер.

config/org.yaml задаёт только начальный список: при первом запуске он переносится в БД.
Бот и дашборд — разные процессы, поэтому каждый периодически перечитывает корпуса (refresh),
а код читает их из org.buildings как раньше.
"""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Building as BuildingCfg
from app.config import OrgConfig
from app.models import BuildingRow, Request

REFRESH_SECONDS = 10

_loaded_at: dict[int, float] = {}


class BuildingError(Exception):
    """Сообщение для пользователя — в тексте исключения."""


async def load(s: AsyncSession, org_id: str) -> list[BuildingRow]:
    return list(await s.scalars(
        select(BuildingRow).where(BuildingRow.org_id == org_id).order_by(BuildingRow.sort_order, BuildingRow.id)
    ))


async def seed_from_config(s: AsyncSession, org: OrgConfig) -> None:
    """Первый запуск: корпуса из конфига → БД. Дальше конфиг их не перезаписывает."""
    if await load(s, org.org_id):
        return
    for order, b in enumerate(org.buildings):
        s.add(BuildingRow(org_id=org.org_id, code=b.code, title=b.title, active=True, sort_order=order))
    await s.flush()


async def refresh(db, org: OrgConfig, *, force: bool = False) -> None:
    """Перечитать корпуса из БД в org.buildings (не чаще раза в REFRESH_SECONDS)."""
    key = id(org)
    if not force and time.monotonic() - _loaded_at.get(key, 0) < REFRESH_SECONDS:
        return
    async with db.session() as s:
        rows = await load(s, org.org_id)
    if rows:
        org.buildings = [BuildingCfg(code=r.code, title=r.title, active=r.active) for r in rows]
    _loaded_at[key] = time.monotonic()


def _new_code(existing: set[str]) -> str:
    """Служебный код нового корпуса (людям не показывается, хранится в заявках)."""
    n = len(existing) + 1
    while f"b{n}" in existing:
        n += 1
    return f"b{n}"


async def _check_title(s: AsyncSession, org_id: str, title: str, code: str | None = None) -> str:
    title = title.strip()
    if len(title) < 2:
        raise BuildingError("Название слишком короткое")
    # Название — это и текст кнопки в боте: длинное в кнопку не поместится
    if len(title) > 40:
        raise BuildingError("Название длиннее 40 символов — в кнопке бота оно не поместится")
    if any(r.title.lower() == title.lower() and r.code != code for r in await load(s, org_id)):
        raise BuildingError("Корпус с таким названием уже есть")
    return title


async def create(s: AsyncSession, org_id: str, *, title: str) -> BuildingRow:
    title = await _check_title(s, org_id, title)
    rows = await load(s, org_id)
    row = BuildingRow(
        org_id=org_id, code=_new_code({r.code for r in rows}), title=title, active=True,
        sort_order=max((r.sort_order for r in rows), default=0) + 1,
    )
    s.add(row)
    await s.flush()
    return row


async def _get(s: AsyncSession, org_id: str, code: str) -> BuildingRow:
    row = await s.scalar(select(BuildingRow).where(BuildingRow.org_id == org_id, BuildingRow.code == code))
    if row is None:
        raise BuildingError("Корпус не найден")
    return row


async def _ensure_other_active(s: AsyncSession, org_id: str, code: str) -> None:
    if not any(r.active and r.code != code for r in await load(s, org_id)):
        raise BuildingError("Нужен хотя бы один активный корпус — иначе в боте нечего выбрать")


async def update(s: AsyncSession, org_id: str, code: str, changes: dict) -> BuildingRow:
    row = await _get(s, org_id, code)
    if "title" in changes:
        row.title = await _check_title(s, org_id, changes["title"], code)
    if "active" in changes and changes["active"] != row.active:
        if not changes["active"]:
            await _ensure_other_active(s, org_id, code)
        row.active = changes["active"]
    await s.flush()
    return row


async def move(s: AsyncSession, org_id: str, code: str, direction: int) -> None:
    """Сдвинуть корпус в списке бота на одну позицию вверх (-1) или вниз (+1)."""
    rows = await load(s, org_id)
    i = next((k for k, r in enumerate(rows) if r.code == code), None)
    if i is None:
        raise BuildingError("Корпус не найден")
    j = i + direction
    if not 0 <= j < len(rows):
        return
    rows[i], rows[j] = rows[j], rows[i]
    for order, r in enumerate(rows):
        r.sort_order = order
    await s.flush()


async def delete(s: AsyncSession, org_id: str, code: str) -> dict:
    row = await _get(s, org_id, code)
    await _ensure_other_active(s, org_id, code)
    used = await s.scalar(
        select(func.count()).select_from(Request).where(Request.org_id == org_id, Request.building == code)
    )
    if used:
        # Старые заявки ссылаются на корпус — только отключаем, название сохранится
        row.active = False
        return {"deleted": False, "archived": True}
    await s.delete(row)
    return {"deleted": True, "archived": False}
