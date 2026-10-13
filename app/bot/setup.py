"""Сборка диспетчера бота — общая для запуска и тестов."""

from __future__ import annotations

from maxapi import Dispatcher
from maxapi.context import SimpleEventIsolation

from app.bot.dispatch import build_dispatch_router
from app.bot.gate import build_gate_router
from app.bot.handlers import build_router
from app.bot.staff_menu import build_staff_router
from app.categorizer import Categorizer
from app.config import OrgConfig
from app.db import Database
from app.membership import MembershipChecker


def create_dispatcher(org: OrgConfig, db: Database, members: MembershipChecker | None = None) -> Dispatcher:
    members = members or MembershipChecker(org.staff_chat_id)
    # Изоляция: события одного пользователя обрабатываются по очереди (нет гонок в FSM).
    dp = Dispatcher(event_isolation=SimpleEventIsolation())
    dp.include_routers(
        # Шлагбаум первым: посторонние дальше не проходят.
        build_gate_router(org, db, members),
        # Роутеры карточек и специалистов раньше заявителя: у него есть обработчики «всё остальное».
        build_dispatch_router(org, db, members),
        build_staff_router(org, db),
        build_router(org, db, Categorizer(org.categories), members),
    )
    return dp
