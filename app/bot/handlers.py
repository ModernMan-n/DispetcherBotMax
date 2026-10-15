"""Сценарий подачи заявки заявителем."""

from __future__ import annotations

import logging

from maxapi import F, Router
from maxapi.context import BaseContext
from maxapi.filters.command import Command, CommandStart
from maxapi.enums.chat_type import ChatType
from maxapi.filters.filter import BaseFilter
from maxapi.filters.state import StateFilter
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types.users import User

from app import repo, staff
from app.bot import ui
from app.bot.dispatch import post_new_request
from app.bot.ui import Action, BuildingChoice, NewRequest
from app.categorizer import Categorizer
from app.config import OrgConfig
from app.db import Database
from app.membership import MembershipChecker
from app.models import Role, Status

log = logging.getLogger(__name__)

ROOM_MAX_LEN = 20
DESCRIPTION_MIN_LEN = 5
DESCRIPTION_MAX_LEN = 1000

STATUS_TITLES = {Status.NEW: "ожидает", Status.IN_PROGRESS: "в работе"}


def display_name(user: User) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p)[:128]


class InDialog(BaseFilter):
    """Сценарий заявителя работает только в личном диалоге, не в групповых чатах."""

    async def __call__(self, event) -> bool:
        message = getattr(event, "message", None)
        if message is None:
            return True  # bot_started и подобные события — всегда из диалога
        return message.recipient.chat_type == ChatType.DIALOG


async def close_buttons(event: MessageCallback) -> None:
    """Отвечает на нажатие, убирая кнопки из исходного сообщения.

    MAX не принимает пустой ответ на callback: нужен message или notification.
    """
    body = event.message.body if event.message else None
    if body is not None and body.text:
        await event.edit(text=body.text, attachments=[])
    else:
        await event.ack(notification="Принято")


def build_router(
    org: OrgConfig, db: Database, members: MembershipChecker
) -> Router:
    router = Router(router_id="applicant")
    router.filter(InDialog())

    async def menu(user_id: int):
        """Главное меню: у специалиста — со своими кнопками."""
        async with db.session() as s:
            specialist = await staff.get_specialist(s, org.org_id, user_id)
        return ui.main_menu(specialist)

    async def greeting(user_id: int) -> str:
        if await is_specialist(user_id):
            return org.text("specialist_welcome")
        return welcome_text()

    def welcome_text() -> str:
        return f"{org.text('welcome')}\n\n{org.text('privacy')}"

    async def is_specialist(user_id: int) -> bool:
        """Специалист (не диспетчер и не админ) заявки не подаёт — только выполняет."""
        async with db.session() as s:
            a = await staff.get_specialist(s, org.org_id, user_id)
        return a is not None and a.role == Role.SPECIALIST

    async def start_flow(context: BaseContext, send, user_id: int) -> None:
        await context.clear()
        if await is_specialist(user_id):
            await send(text=org.text("specialist_no_requests"), attachments=[await menu(user_id)])
            return
        await context.set_state(NewRequest.building)
        await send(text=org.text("ask_building"), attachments=[ui.buildings_keyboard(org)])

    async def my_requests_text(user_id: int) -> str:
        async with db.session() as s:
            items = await repo.list_active_for_applicant(s, org.org_id, user_id)
        if not items:
            return org.text("no_requests")
        lines = ["Ваши активные заявки:"]
        for r in items:
            building = org.building_titles.get(r.building, r.building)
            lines.append(f"№{r.id} — {building}, каб. {r.room} — {STATUS_TITLES[Status(r.status)]}")
        return "\n".join(lines)

    # --- Вход и команды (регистрируются первыми, чтобы работать в любом состоянии) ---

    @router.bot_started()
    async def on_bot_started(event: BotStarted, context: BaseContext):
        await context.clear()
        user_id = event.user.user_id
        await event.bot.send_message(
            chat_id=event.chat_id, text=await greeting(user_id), attachments=[await menu(user_id)]
        )

    @router.message_created(CommandStart())
    async def on_start(event: MessageCreated, context: BaseContext):
        await context.clear()
        user_id = event.message.sender.user_id
        await event.message.answer(text=await greeting(user_id), attachments=[await menu(user_id)])

    @router.message_created(Command(["new", "zayavka"]))
    async def on_new_command(event: MessageCreated, context: BaseContext):
        await start_flow(context, event.message.answer, event.message.sender.user_id)

    @router.message_created(Command(["cancel", "otmena"]))
    async def on_cancel_command(event: MessageCreated, context: BaseContext):
        await context.clear()
        await event.message.answer(text=org.text("cancelled"), attachments=[await menu(event.message.sender.user_id)])

    @router.message_created(Command(["my"]))
    async def on_my_command(event: MessageCreated):
        await event.message.answer(
            text=await my_requests_text(event.message.sender.user_id), attachments=[await menu(event.message.sender.user_id)]
        )

    # --- Кнопки меню ---

    @router.message_callback(Action.filter(F.name == "new"))
    async def on_new_button(event: MessageCallback, context: BaseContext):
        await close_buttons(event)
        await start_flow(context, event.send, event.callback.user.user_id)

    @router.message_callback(Action.filter(F.name == "my"))
    async def on_my_button(event: MessageCallback):
        await close_buttons(event)
        await event.send(
            text=await my_requests_text(event.callback.user.user_id), attachments=[await menu(event.callback.user.user_id)]
        )

    @router.message_callback(Action.filter(F.name == "cancel"))
    async def on_cancel_button(event: MessageCallback, context: BaseContext):
        await context.clear()
        await event.edit(text=org.text("cancelled"), attachments=[])
        await event.send(text=org.text("idle"), attachments=[await menu(event.callback.user.user_id)])

    # --- Шаги сценария ---

    @router.message_callback(BuildingChoice.filter(), NewRequest.building)
    async def on_building(event: MessageCallback, context: BaseContext, payload: BuildingChoice):
        title = org.building_titles.get(payload.code)
        if title is None:
            await event.ack(notification="Такого корпуса нет, выберите из списка")
            return
        await context.update_data(building=payload.code)
        await context.set_state(NewRequest.room)
        await event.edit(text=f"Корпус: {title}", attachments=[])
        await event.send(text=org.text("ask_room"), attachments=[ui.cancel_keyboard()])

    @router.message_created(F.message.body.text, NewRequest.room)
    async def on_room(event: MessageCreated, context: BaseContext):
        room = event.message.body.text.strip()
        if not room or len(room) > ROOM_MAX_LEN or "\n" in room:
            await event.message.answer(text=org.text("bad_room"), attachments=[ui.cancel_keyboard()])
            return
        await context.update_data(room=room)
        await context.set_state(NewRequest.description)
        await event.message.answer(text=org.text("ask_description"), attachments=[ui.cancel_keyboard()])

    @router.message_created(F.message.body.text, NewRequest.description)
    async def on_description(event: MessageCreated, context: BaseContext):
        description = event.message.body.text.strip()
        if len(description) < DESCRIPTION_MIN_LEN:
            await event.message.answer(
                text=org.text("bad_description"), attachments=[ui.cancel_keyboard()]
            )
            return
        description = description[:DESCRIPTION_MAX_LEN]
        guess = Categorizer(org.active_categories).guess(description)
        data = await context.update_data(description=description, category=guess.code)
        await context.set_state(NewRequest.confirm)
        await event.message.answer(
            text=org.text(
                "confirm",
                building=org.building_titles[data["building"]],
                room=data["room"],
                description=description,
                category=org.category_titles[guess.code],
            ),
            attachments=[ui.confirm_keyboard()],
        )

    @router.message_callback(Action.filter(F.name == "send"), NewRequest.confirm)
    async def on_send(event: MessageCallback, context: BaseContext):
        data = await context.get_data()
        # Сначала сбрасываем состояние, чтобы повторное нажатие не создало дубль.
        await context.clear()
        user = event.callback.user
        async with db.session() as s, s.begin():
            req = await repo.create_request(
                s,
                org_id=org.org_id,
                building=data["building"],
                room=data["room"],
                description=data["description"],
                category=data["category"],
                applicant_max_id=user.user_id,
                applicant_name=display_name(user),
                sla_hours=org.category_sla.get(data["category"]),
            )
        log.info("Создана заявка №%s (%s)", req.id, req.category)
        await close_buttons(event)
        await event.send(text=org.text("created", id=req.id), attachments=[await menu(event.callback.user.user_id)])
        await post_new_request(event.bot, db, org, req, members)

    # --- Всё остальное ---

    @router.message_created(NewRequest.confirm)
    async def on_text_in_confirm(event: MessageCreated):
        await event.message.answer(
            text="Нажмите «Отправить» или «Отменить» под заявкой выше.",
            attachments=[ui.confirm_keyboard()],
        )

    @router.message_created(NewRequest.room, NewRequest.description, NewRequest.building)
    async def on_non_text_in_flow(event: MessageCreated):
        await event.message.answer(
            text="Пожалуйста, ответьте текстом или нажмите кнопку выше.",
            attachments=[ui.cancel_keyboard()],
        )

    @router.message_callback()
    async def on_stale_button(event: MessageCallback):
        # Кнопка из старого сообщения, когда сценарий уже ушёл дальше.
        await event.ack(notification="Эта кнопка уже неактуальна")

    @router.message_created(StateFilter(None))
    async def on_idle(event: MessageCreated):
        await event.message.answer(text=org.text("idle"), attachments=[await menu(event.message.sender.user_id)])

    return router
