"""Окружение Alembic.

Два способа запуска:
- из командной строки (`alembic upgrade head`, `alembic revision --autogenerate`) — адрес БД
  берётся из DATABASE_URL / .env, как у бота;
- из кода приложения (app.db.migrate) — тогда соединение уже передано в config.attributes.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import load_settings
from app.db import resolve_url
from app.models import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    # Свой тип времени в миграциях пишем обычным DateTime(timezone=True) — миграции не зависят от кода моделей.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite не умеет большинство ALTER — Alembic пересоздаёт таблицу
        compare_type=True,
        render_item=render_item,
    )


def run_on_connection(connection) -> None:
    configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(resolve_url(load_settings().database_url))
    async with engine.connect() as conn:
        await conn.run_sync(run_on_connection)
        await conn.commit()
    await engine.dispose()


def run_offline() -> None:
    context.configure(
        url=resolve_url(load_settings().database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    # Вызов из приложения: логирование не трогаем, соединение и транзакция уже есть.
    run_on_connection(connection)
elif context.is_offline_mode():
    run_offline()
else:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    asyncio.run(run_async())

