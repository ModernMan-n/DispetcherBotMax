"""Категории заявок (они же направления специалистов) — в БД, ими управляет диспетчер.

config/org.yaml задаёт только начальный набор: при первом запуске он переносится в БД.
Бот и дашборд — разные процессы, поэтому каждый периодически перечитывает категории из БД
(refresh), а код читает их из org.categories как раньше.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Category as CategoryCfg
from app.config import OrgConfig
from app.models import Category, Request

REFRESH_SECONDS = 10
DEFAULT_SLA_HOURS = 48

_loaded_at: dict[int, float] = {}


class CategoryError(Exception):
    """Сообщение для пользователя — в тексте исключения."""


def _to_cfg(row: Category) -> CategoryCfg:
    return CategoryCfg(
        code=row.code, title=row.name, sla_hours=row.sla_hours,
        keywords=json.loads(row.keywords or "[]"), fallback=row.fallback, active=row.active,
    )


async def load(s: AsyncSession, org_id: str) -> list[Category]:
    return list(await s.scalars(
        select(Category).where(Category.org_id == org_id).order_by(Category.sort_order, Category.id)
    ))


async def seed_from_config(s: AsyncSession, org: OrgConfig) -> None:
    """Первый запуск: категории из конфига → БД. Дальше конфиг их не перезаписывает.

    Заодно дозаполняет категории, созданные до появления ключевых слов в БД (keywords IS NULL).
    """
    rows = {c.code: c for c in await load(s, org.org_id)}
    for order, c in enumerate(org.categories):
        row = rows.get(c.code)
        if row is None and not rows:
            s.add(Category(
                org_id=org.org_id, code=c.code, name=c.title, sla_hours=c.sla_hours,
                keywords=json.dumps(c.keywords, ensure_ascii=False), fallback=c.fallback,
                active=True, sort_order=order,
            ))
        elif row is not None and row.keywords is None:
            row.keywords = json.dumps(c.keywords, ensure_ascii=False)
            row.fallback, row.sort_order = c.fallback, order
    await s.flush()


async def refresh(db, org: OrgConfig, *, force: bool = False) -> None:
    """Перечитать категории из БД в org.categories (не чаще раза в REFRESH_SECONDS)."""
    key = id(org)
    if not force and time.monotonic() - _loaded_at.get(key, 0) < REFRESH_SECONDS:
        return
    async with db.session() as s:
        rows = await load(s, org.org_id)
    if rows and any(r.fallback for r in rows):
        org.categories = [_to_cfg(r) for r in rows]
    _loaded_at[key] = time.monotonic()


# --- управление (диспетчер) ---


def _parse_keywords(raw: list[str]) -> list[str]:
    words = []
    for w in raw:
        w = w.strip().lower().replace("ё", "е")
        if w and w not in words:
            words.append(w[:64])
    return words[:100]


def _new_code(existing: set[str]) -> str:
    """Служебный код для новой категории (названия русские, код людям не показывается)."""
    n = len(existing) + 1
    while f"c{n}" in existing:
        n += 1
    return f"c{n}"


async def create(s: AsyncSession, org_id: str, *, title: str, sla_hours: int, keywords: list[str]) -> Category:
    rows = await load(s, org_id)
    if any(r.name.lower() == title.lower() for r in rows):
        raise CategoryError("Категория с таким названием уже есть")
    code = _new_code({r.code for r in rows})
    row = Category(
        org_id=org_id, code=code, name=title, sla_hours=sla_hours,
        keywords=json.dumps(_parse_keywords(keywords), ensure_ascii=False),
        fallback=False, active=True, sort_order=max((r.sort_order for r in rows), default=0) + 1,
    )
    s.add(row)
    await s.flush()
    return row


async def update(s: AsyncSession, org_id: str, code: str, changes: dict) -> Category:
    row = await s.scalar(select(Category).where(Category.org_id == org_id, Category.code == code))
    if row is None:
        raise CategoryError("Категория не найдена")
    if "title" in changes:
        title = changes["title"].strip()
        others = await load(s, org_id)
        if any(r.name.lower() == title.lower() and r.code != code for r in others):
            raise CategoryError("Категория с таким названием уже есть")
        row.name = title
    if "sla_hours" in changes:
        row.sla_hours = changes["sla_hours"]
    if "keywords" in changes:
        row.keywords = json.dumps(_parse_keywords(changes["keywords"]), ensure_ascii=False)
    if "active" in changes:
        if row.fallback and not changes["active"]:
            raise CategoryError("Категорию «по умолчанию» отключить нельзя: в неё попадают нераспознанные заявки")
        row.active = changes["active"]
    await s.flush()
    return row


async def delete(s: AsyncSession, org_id: str, code: str) -> dict:
    row = await s.scalar(select(Category).where(Category.org_id == org_id, Category.code == code))
    if row is None:
        raise CategoryError("Категория не найдена")
    if row.fallback:
        raise CategoryError("Категорию «по умолчанию» удалить нельзя")
    used = await s.scalar(
        select(func.count()).select_from(Request).where(Request.org_id == org_id, Request.category == code)
    )
    if used:
        # Старые заявки и аналитика ссылаются на неё — только отключаем
        row.active = False
        return {"deleted": False, "archived": True}
    await s.delete(row)
    return {"deleted": True, "archived": False}


# --- сроки ---


def default_due(org: OrgConfig, category: str, created_at):
    return created_at + timedelta(hours=org.category_sla.get(category, DEFAULT_SLA_HOURS))


async def backfill_due_dates(s: AsyncSession, org: OrgConfig) -> int:
    """Заявкам, созданным до появления сроков, ставим срок по нормативу категории."""
    rows = list(await s.scalars(select(Request).where(Request.org_id == org.org_id, Request.due_at.is_(None))))
    for r in rows:
        r.due_at = default_due(org, r.category, r.created_at)
    await s.flush()
    return len(rows)

