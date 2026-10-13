"""Подключение к БД. Смена SQLite на PostgreSQL — это смена DATABASE_URL.

Схема создаётся и обновляется миграциями Alembic (папка migrations/) при каждом запуске
бота и дашборда. Новая миграция: `uv run alembic revision --autogenerate -m "что изменилось"`.
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import ROOT, OrgConfig
from app.models import Category


def resolve_url(url: str) -> str:
    """Относительный путь SQLite считаем от корня проекта, а не от текущей папки."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        path = ROOT / parsed.database if not parsed.database.startswith("/") else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            parsed = parsed.set(database=str(path))
    return parsed.render_as_string(hide_password=False)


class SchemaError(RuntimeError):
    pass


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _upgrade(conn: Connection) -> None:
    tables = set(inspect(conn).get_table_names())
    if "requests" in tables and "alembic_version" not in tables:
        # База создана до Alembic: молча «догонять» её опасно — схема может отличаться.
        raise SchemaError(
            "База создана до подключения миграций. Если её схема актуальна, отметьте это командой "
            "`uv run alembic stamp head` и запустите снова."
        )
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "head")


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(resolve_url(url))
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def migrate(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(_upgrade)

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
    await db.migrate()
    await db.sync_categories(org)
    return db
