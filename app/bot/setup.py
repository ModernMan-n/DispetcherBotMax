"""Сборка диспетчера бота — общая для запуска и тестов."""

from __future__ import annotations

from maxapi import Dispatcher
from maxapi.context import SimpleEventIsolation

from app.bot.dispatch import build_dispatch_router
from app.bot.handlers import build_router
from app.categorizer import Categorizer
from app.config import OrgConfig
from app.db import Database


def create_dispatcher(org: OrgConfig, db: Database) -> Dispatcher:
    # Изоляция: события одного пользователя обрабатываются по очереди (нет гонок в FSM).
    dp = Dispatcher(event_isolation=SimpleEventIsolation())
    dp.include_routers(
        # Роутер диспетчерской первым: у роутера заявителя есть обработчики «всё остальное».
        build_dispatch_router(org, db),
        build_router(org, db, Categorizer(org.categories)),
    )
    return dp
