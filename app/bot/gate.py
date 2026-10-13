"""Доступ к боту — только участникам чата «Сотрудники». Роутер подключается первым.

Проверяются события из личного диалога с ботом. Групповые чаты (диспетчерская) сюда
не попадают: туда людей добавляет администратор чата.
"""

from __future__ import annotations

import logging

from maxapi import Router
from maxapi.enums.chat_type import ChatType
from maxapi.filters.filter import BaseFilter
from maxapi.types import BotStarted, MessageCallback, MessageCreated, UserAdded, UserRemoved

from app import staff
from app.config import OrgConfig
from app.db import Database
from app.membership import MembershipChecker

log = logging.getLogger(__name__)


def dialog_user_id(event) -> int | None:
    """ID пользователя, если событие пришло из личного диалога с ботом."""
    if isinstance(event, BotStarted):
        return event.user.user_id
    if isinstance(event, MessageCreated):
        if event.message.recipient.chat_type == ChatType.DIALOG and event.message.sender:
            return event.message.sender.user_id
        return None
    if isinstance(event, MessageCallback):
        if event.message is None or event.message.recipient.chat_type == ChatType.DIALOG:
            return event.callback.user.user_id
        return None
    return None


class Outsider(BaseFilter):
    """Срабатывает на события из лички от тех, кого нет в чате «Сотрудники»."""

    def __init__(self, members: MembershipChecker) -> None:
        self.members = members

    async def __call__(self, event) -> bool:
        if not self.members.enabled:
            return False
        user_id = dialog_user_id(event)
        if user_id is None:
            return False
        if await self.members.is_member(event.bot, user_id):
            return False
        log.info("Отказ в доступе: пользователь %s не в чате сотрудников", user_id)
        return True


def build_gate_router(org: OrgConfig, db: Database, members: MembershipChecker) -> Router:
    router = Router(router_id="gate")
    outsider = Outsider(members)

    @router.bot_started(outsider)
    async def deny_start(event: BotStarted):
        await event.bot.send_message(chat_id=event.chat_id, text=org.text("access_denied"))

    @router.message_created(outsider)
    async def deny_message(event: MessageCreated):
        await event.message.answer(text=org.text("access_denied"))

    @router.message_callback(outsider)
    async def deny_button(event: MessageCallback):
        await event.ack(notification="Бот доступен только сотрудникам")

    def in_staff_chat(event) -> bool:
        return members.enabled and event.chat_id == members.chat_id

    @router.user_added()
    async def on_user_added(event: UserAdded):
        if in_staff_chat(event):
            members.joined(event.user.user_id)

    @router.user_removed()
    async def on_user_removed(event: UserRemoved):
        if not in_staff_chat(event):
            return
        user_id = event.user.user_id
        members.left(user_id)
        # Специалист ушёл из сотрудников — снимаем с линии, чтобы заявки ему больше не приходили.
        async with db.session() as s, s.begin():
            a = await staff.get_specialist(s, org.org_id, user_id)
            if a is not None and a.on_duty:
                a.on_duty = False
                log.info("Специалист %s покинул чат сотрудников — снят с линии", a.id)

    return router
