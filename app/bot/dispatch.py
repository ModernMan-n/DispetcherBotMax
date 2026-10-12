"""Чат диспетчерской: карточки заявок с кнопками и уведомления заявителю."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from maxapi import Bot, F, Router
from maxapi.enums.intent import Intent
from maxapi.filters.callback_payload import CallbackPayload
from maxapi.filters.filter import BaseFilter
from maxapi.types import BotAdded, MessageCallback
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from app import repo
from app.config import OrgConfig
from app.db import Database
from app.formatting import format_duration
from app.models import ACTIVE_STATUSES, TRANSITIONS, Request, Status

log = logging.getLogger(__name__)

STATUS_LINE = {
    Status.NEW: "🆕 Новая",
    Status.IN_PROGRESS: "🔧 В работе",
    Status.DONE: "✅ Выполнена",
    Status.CANCELLED: "✖️ Отменена",
}

NOTIFY_TEXT_KEY = {
    Status.IN_PROGRESS: "notify_in_progress",
    Status.DONE: "notify_done",
    Status.CANCELLED: "notify_cancelled",
}


class Card(CallbackPayload, prefix="r"):
    act: str  # take | done | release | cancel | cancel_ok | cat | back
    id: int


class CardCategory(CallbackPayload, prefix="rc"):
    id: int
    code: str


class FromChat(BaseFilter):
    """Нажатия принимаются только из чата диспетчерской."""

    def __init__(self, chat_id: int | None) -> None:
        self.chat_id = chat_id

    async def __call__(self, event) -> bool:
        message = getattr(event, "message", None)
        return (
            self.chat_id is not None
            and message is not None
            and message.recipient.chat_id == self.chat_id
        )


def render_card(org: OrgConfig, req: Request) -> str:
    tz = ZoneInfo(org.timezone)

    def local(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%H:%M %d.%m")

    status = Status(req.status)
    lines = [
        f"Заявка №{req.id} · {org.category_titles.get(req.category, req.category)}",
        f"{org.building_titles.get(req.building, req.building)}, каб. {req.room}",
        req.description,
        f"От: {req.applicant_name}",
        f"Создана: {local(req.created_at)}",
        "",
    ]
    status_line = STATUS_LINE[status]
    if status == Status.IN_PROGRESS and req.assignee:
        status_line += f" — {req.assignee.name}, с {local(req.taken_at)}"
    elif status == Status.DONE:
        who = f" — {req.assignee.name}" if req.assignee else ""
        status_line += f"{who}, за {format_duration(req.closed_at - req.created_at)}"
    lines.append(status_line)
    return "\n".join(lines)


def card_keyboard(req: Request) -> list:
    status = Status(req.status)
    kb = InlineKeyboardBuilder()

    def btn(text: str, act: str, intent: Intent = Intent.DEFAULT) -> CallbackButton:
        return CallbackButton(text=text, payload=Card(act=act, id=req.id).pack(), intent=intent)

    if status == Status.NEW:
        kb.row(btn("Взять", "take", Intent.POSITIVE))
    elif status == Status.IN_PROGRESS:
        kb.row(btn("Готово", "done", Intent.POSITIVE), btn("Вернуть в очередь", "release"))
    else:
        return []  # закрытая заявка — без кнопок
    kb.row(btn("Категория", "cat"), btn("Отменить", "cancel", Intent.NEGATIVE))
    return [kb.as_markup()]


def cancel_confirm_keyboard(req: Request) -> list:
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="Да, отменить", payload=Card(act="cancel_ok", id=req.id).pack(), intent=Intent.NEGATIVE
        ),
        CallbackButton(text="Назад", payload=Card(act="back", id=req.id).pack()),
    )
    return [kb.as_markup()]


def category_keyboard(org: OrgConfig, req: Request) -> list:
    kb = InlineKeyboardBuilder()
    for c in org.categories:
        mark = "• " if c.code == req.category else ""
        kb.add(CallbackButton(text=mark + c.title, payload=CardCategory(id=req.id, code=c.code).pack()))
    kb.adjust(2)
    kb.row(CallbackButton(text="Назад", payload=Card(act="back", id=req.id).pack()))
    return [kb.as_markup()]


async def post_new_request(bot: Bot, org: OrgConfig, req: Request) -> None:
    """Отправляет карточку новой заявки в чат диспетчерской. Ошибки не мешают заявителю."""
    if org.dispatcher_chat_id is None:
        log.warning("dispatcher_chat_id не задан — заявка №%s не отправлена в диспетчерскую", req.id)
        return
    try:
        await bot.send_message(
            chat_id=org.dispatcher_chat_id, text=render_card(org, req), attachments=card_keyboard(req)
        )
    except Exception:
        log.exception("Не удалось отправить заявку №%s в чат диспетчерской", req.id)


async def notify_applicant(bot: Bot, org: OrgConfig, req: Request) -> None:
    key = NOTIFY_TEXT_KEY.get(Status(req.status))
    if key is None:
        return
    try:
        await bot.send_message(user_id=req.applicant_max_id, text=org.text(key, id=req.id))
    except Exception:
        log.exception("Не удалось уведомить заявителя по заявке №%s", req.id)


def build_dispatch_router(org: OrgConfig, db: Database) -> Router:
    router = Router(router_id="dispatch")
    in_chat = FromChat(org.dispatcher_chat_id)
    # Один процесс бота (см. lock в __main__), поэтому обычной блокировки достаточно,
    # чтобы двое не взяли одну заявку одновременно.
    status_lock = asyncio.Lock()

    @router.bot_added()
    async def on_bot_added(event: BotAdded):
        if event.chat_id == org.dispatcher_chat_id:
            text = "Чат диспетчерской подключён. Новые заявки будут приходить сюда."
        else:
            text = (
                f"ID этого чата: {event.chat_id}\n"
                f"Чтобы сюда приходили заявки, укажите в .env:\n"
                f"DISPATCHER_CHAT_ID={event.chat_id}\n"
                f"и перезапустите бота."
            )
        log.info("Бота добавили в чат %s", event.chat_id)
        await event.bot.send_message(chat_id=event.chat_id, text=text)

    async def change_status(event: MessageCallback, req_id: int, status: Status) -> None:
        user = event.callback.user
        name = " ".join(p for p in (user.first_name, user.last_name) if p)[:128]
        async with status_lock, db.session() as s, s.begin():
            req = await repo.get_request(s, org.org_id, req_id)
            if req is None:
                await event.ack(notification="Заявка не найдена")
                return
            if status not in TRANSITIONS[Status(req.status)]:
                # Кто-то успел раньше — просто показываем актуальное состояние.
                await event.edit(text=render_card(org, req), attachments=card_keyboard(req))
                return
            assignee_id = None
            if status == Status.IN_PROGRESS:
                assignee_id = (await repo.get_or_create_assignee(s, org.org_id, user.user_id, name)).id
            await repo.set_status(s, req, status, changed_by=user.user_id, assignee_id=assignee_id)
            await s.refresh(req, ["assignee"])
        log.info("Заявка №%s → %s (%s)", req.id, status, user.user_id)
        await event.edit(text=render_card(org, req), attachments=card_keyboard(req))
        await notify_applicant(event.bot, org, req)

    @router.message_callback(Card.filter(F.act == "take"), in_chat)
    async def on_take(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.IN_PROGRESS)

    @router.message_callback(Card.filter(F.act == "done"), in_chat)
    async def on_done(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.DONE)

    @router.message_callback(Card.filter(F.act == "release"), in_chat)
    async def on_release(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.NEW)

    @router.message_callback(Card.filter(F.act == "cancel_ok"), in_chat)
    async def on_cancel_ok(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.CANCELLED)

    async def show(event: MessageCallback, req_id: int, keyboard) -> None:
        async with db.session() as s:
            req = await repo.get_request(s, org.org_id, req_id)
        if req is None:
            await event.ack(notification="Заявка не найдена")
            return
        attachments = keyboard(req) if Status(req.status) in ACTIVE_STATUSES else []
        await event.edit(text=render_card(org, req), attachments=attachments)

    @router.message_callback(Card.filter(F.act == "cancel"), in_chat)
    async def on_cancel(event: MessageCallback, payload: Card):
        await show(event, payload.id, cancel_confirm_keyboard)

    @router.message_callback(Card.filter(F.act == "cat"), in_chat)
    async def on_category_menu(event: MessageCallback, payload: Card):
        await show(event, payload.id, lambda req: category_keyboard(org, req))

    @router.message_callback(Card.filter(F.act == "back"), in_chat)
    async def on_back(event: MessageCallback, payload: Card):
        await show(event, payload.id, card_keyboard)

    @router.message_callback(CardCategory.filter(), in_chat)
    async def on_category_set(event: MessageCallback, payload: CardCategory):
        if payload.code not in org.category_titles:
            await event.ack(notification="Нет такой категории")
            return
        async with db.session() as s, s.begin():
            req = await repo.get_request(s, org.org_id, payload.id)
            if req is None:
                await event.ack(notification="Заявка не найдена")
                return
            await repo.set_category(s, req, payload.code)
        await event.edit(text=render_card(org, req), attachments=card_keyboard(req))

    return router
