"""Миграции Alembic: схема из миграций совпадает с моделями, откат работает."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db import Database, SchemaError, alembic_config, open_db
from app.models import Base


def _upgrade_sync(url: str, target: str = "head") -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, target)
    engine.dispose()


def test_models_match_migrations(tmp_path):
    """Если поменяли модели и забыли `alembic revision --autogenerate` — этот тест упадёт."""
    url = f"sqlite:///{tmp_path / 'm.db'}"
    _upgrade_sync(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == [], f"Модели и миграции расходятся: {diff}"


def test_downgrade_to_empty(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    _upgrade_sync(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    engine.dispose()


async def test_open_db_is_idempotent(tmp_path, org):
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    for _ in range(2):  # второй запуск ничего не ломает
        db = await open_db(url, org)
        await db.close()


async def test_pre_alembic_db_is_refused(tmp_path, org):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    db = Database(url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # как создавались базы до Alembic
    with pytest.raises(SchemaError, match="alembic stamp head"):
        await db.migrate()
    await db.close()
