"""Подключение к БД. Смена SQLite на PostgreSQL — это смена DATABASE_URL."""

from __future__ import annotations

from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import ROOT, OrgConfig
from app.models import Base, Category


def _resolve_url(url: str) -> str:
    """Относительный путь SQLite считаем от корня проекта, а не от текущей папки."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        path = ROOT / parsed.database if not parsed.database.startswith("/") else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            parsed = parsed.set(database=str(path))
    return parsed.render_as_string(hide_password=False)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(_resolve_url(url))
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        # На пилоте хватает create_all. При переезде на PostgreSQL подключить Alembic.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def sync_categories(self, org: OrgConfig) -> None:
        """Справочник категорий в БД повторяет конфиг (нужен дашборду и для ручной правки)."""
        async with self.session() as s, s.begin():
            existing = {
                c.code: c
                for c in await s.scalars(select(Category).where(Category.org_id == org.org_id))
            }
            for c in org.categories:
                row = existing.get(c.code)
                if row is None:
                    s.add(Category(org_id=org.org_id, code=c.code, name=c.title, sla_hours=c.sla_hours))
                else:
                    row.name, row.sla_hours = c.title, c.sla_hours

    async def close(self) -> None:
        await self.engine.dispose()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


async def open_db(url: str, org: OrgConfig) -> Database:
    db = Database(url)
    await db.create_schema()
    await db.sync_categories(org)
    return db
