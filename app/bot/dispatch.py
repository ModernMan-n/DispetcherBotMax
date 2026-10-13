"""Карточки заявок: чат диспетчерской и личка специалистов, кнопки и уведомления заявителю.

Карточка одной заявки может лежать в нескольких местах (групповой чат, личка каждого
специалиста направления). Все копии запоминаются в card_messages и обновляются вместе.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from maxapi import Bot, F, Router
from maxapi.enums.chat_type import ChatType
from maxapi.enums.intent import Intent
from maxapi.filters.callback_payload import CallbackPayload
from maxapi.filters.filter import BaseFilter
from maxapi.types import BotAdded, MessageCallback
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from app import repo, staff
from app.config import OrgConfig
from app.db import Database
from app.formatting import format_duration
from app.membership import MembershipChecker
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
    act: str  # open | take | done | release | cancel | cancel_ok | cat | back
    id: int


class CardCategory(CallbackPayload, prefix="rc"):
    id: int
    code: str


class CardAccess(BaseFilter):
    """Кнопки карточек работают в чате диспетчерской и в личке у специалистов."""

    def __init__(self, org: OrgConfig, db: Database) -> None:
        self.org, self.db = org, db

    async def __call__(self, event) -> bool:
        message = getattr(event, "message", None)
        if message is None:
            return False
        if self.org.dispatcher_chat_id is not None and message.recipient.chat_id == self.org.dispatcher_chat_id:
            return True
        if message.recipient.chat_type != ChatType.DIALOG:
            return False
        async with self.db.session() as s:
            return await staff.get_specialist(s, self.org.org_id, event.callback.user.user_id) is not None


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


# --- рассылка и синхронизация карточек ---


async def _send_card(bot: Bot, db: Database, org: OrgConfig, req: Request, *, chat_id=None, user_id=None) -> None:
    try:
        sent = await bot.send_message(
            chat_id=chat_id, user_id=user_id, text=render_card(org, req), attachments=card_keyboard(req)
        )
    except Exception:
        log.exception("Не удалось отправить карточку №%s (chat=%s, user=%s)", req.id, chat_id, user_id)
        return
    mid = sent.message.body.mid if sent and sent.message and sent.message.body else None
    if mid:
        async with db.session() as s, s.begin():
            await staff.save_card(s, req.id, mid, chat_id=chat_id, user_id=user_id)


async def send_to_specialists(
    bot: Bot, db: Database, org: OrgConfig, req: Request, members: MembershipChecker
) -> None:
    """Карточка — специалистам направления на линии, у которых её ещё нет и кто ещё в сотрудниках."""
    async with db.session() as s:
        recipients = await staff.on_duty_for_category(s, org.org_id, req.category)
        already = {c.user_id for c in await staff.card_messages(s, req.id) if c.user_id}
    for a in recipients:
        if a.max_id in already:
            continue
        if not await members.is_member(bot, a.max_id):
            log.info("Специалист %s не в чате сотрудников — карточку №%s не отправляем", a.id, req.id)
            continue
        await _send_card(bot, db, org, req, user_id=a.max_id)


async def post_new_request(
    bot: Bot, db: Database, org: OrgConfig, req: Request, members: MembershipChecker
) -> None:
    """Новая заявка: в чат диспетчерской и специалистам направления. Ошибки не мешают заявителю."""
    if org.dispatcher_chat_id is not None:
        await _send_card(bot, db, org, req, chat_id=org.dispatcher_chat_id)
    await send_to_specialists(bot, db, org, req, members)


async def sync_cards(bot: Bot, db: Database, org: OrgConfig, req: Request, *, skip_mid: str | None) -> None:
    """Обновляет все копии карточки, кроме той, на которой нажали (её правит сам callback)."""
    async with db.session() as s:
        cards = await staff.card_messages(s, req.id)
    text, attachments = render_card(org, req), card_keyboard(req)
    for c in cards:
        if c.message_id == skip_mid:
            continue
        try:
            await bot.edit_message(message_id=c.message_id, text=text, attachments=attachments)
        except Exception:
            # Старые сообщения MAX может не дать отредактировать — не страшно.
            log.warning("Не удалось обновить карточку №%s (%s)", req.id, c.message_id, exc_info=True)


async def notify_applicant(bot: Bot, org: OrgConfig, req: Request) -> None:
    key = NOTIFY_TEXT_KEY.get(Status(req.status))
    if key is None:
        return
    try:
        await bot.send_message(user_id=req.applicant_max_id, text=org.text(key, id=req.id))
    except Exception:
        log.exception("Не удалось уведомить заявителя по заявке №%s", req.id)


def _pressed_mid(event: MessageCallback) -> str | None:
    body = event.message.body if event.message else None
    return body.mid if body else None


def build_dispatch_router(org: OrgConfig, db: Database, members: MembershipChecker) -> Router:
    router = Router(router_id="dispatch")
    access = CardAccess(org, db)
    # Один процесс бота (см. lock в __main__), поэтому обычной блокировки достаточно,
    # чтобы двое не взяли одну заявку одновременно.
    status_lock = asyncio.Lock()

    @router.bot_added()
    async def on_bot_added(event: BotAdded):
        log.info("Бота добавили в чат %s", event.chat_id)
        if event.chat_id == org.dispatcher_chat_id:
            await event.bot.send_message(
                chat_id=event.chat_id, text="Чат диспетчерской подключён. Новые заявки будут приходить сюда."
            )
            return
        if event.chat_id == members.chat_id:
            return  # чат «Сотрудники»: молчим, там все сотрудники колледжа
        # Незнакомый чат: ID не пишем в сам чат (он может быть большим), а присылаем тому, кто добавил.
        text = (
            f"Меня добавили в чат, ID: {event.chat_id}\n"
            f"Если это чат диспетчерской — укажите в .env DISPATCHER_CHAT_ID={event.chat_id}\n"
            f"Если это чат «Сотрудники» — STAFF_CHAT_ID={event.chat_id}\n"
            f"Затем перезапустите бота."
        )
        try:
            await event.bot.send_message(user_id=event.user.user_id, text=text)
        except Exception:
            log.warning("Не удалось написать в личку тому, кто добавил бота; ID чата есть в этом логе")

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
                # Специалист берёт заявку под своей карточкой, остальные — заводятся по имени из MAX.
                specialist = await staff.get_specialist(s, org.org_id, user.user_id)
                assignee = specialist or await repo.get_or_create_assignee(s, org.org_id, user.user_id, name)
                assignee_id = assignee.id
            try:
                await repo.set_status(s, req, status, changed_by=user.user_id, assignee_id=assignee_id)
            except repo.TransitionError:
                # Заявку только что изменили в другом месте (например, в веб-интерфейсе)
                await s.refresh(req, ["assignee"])
                await event.edit(text=render_card(org, req), attachments=card_keyboard(req))
                return
            await s.refresh(req, ["assignee"])
        log.info("Заявка №%s → %s (%s)", req.id, status, user.user_id)
        await event.edit(text=render_card(org, req), attachments=card_keyboard(req))
        await sync_cards(event.bot, db, org, req, skip_mid=_pressed_mid(event))
        await notify_applicant(event.bot, org, req)

    @router.message_callback(Card.filter(F.act == "take"), access)
    async def on_take(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.IN_PROGRESS)

    @router.message_callback(Card.filter(F.act == "done"), access)
    async def on_done(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.DONE)

    @router.message_callback(Card.filter(F.act == "release"), access)
    async def on_release(event: MessageCallback, payload: Card):
        await change_status(event, payload.id, Status.NEW)

    @router.message_callback(Card.filter(F.act == "cancel_ok"), access)
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

    @router.message_callback(Card.filter(F.act == "open"), access)
    async def on_open(event: MessageCallback, payload: Card):
        """Специалист открыл заявку из списка — присылаем карточку отдельным сообщением."""
        async with db.session() as s:
            req = await repo.get_request(s, org.org_id, payload.id)
        if req is None:
            await event.ack(notification="Заявка не найдена")
            return
        await event.ack(notification=f"Заявка №{req.id}")
        await _send_card(event.bot, db, org, req, user_id=event.callback.user.user_id)

    @router.message_callback(Card.filter(F.act == "cancel"), access)
    async def on_cancel(event: MessageCallback, payload: Card):
        await show(event, payload.id, cancel_confirm_keyboard)

    @router.message_callback(Card.filter(F.act == "cat"), access)
    async def on_category_menu(event: MessageCallback, payload: Card):
        await show(event, payload.id, lambda req: category_keyboard(org, req))

    @router.message_callback(Card.filter(F.act == "back"), access)
    async def on_back(event: MessageCallback, payload: Card):
        await show(event, payload.id, card_keyboard)

    @router.message_callback(CardCategory.filter(), access)
    async def on_category_set(event: MessageCallback, payload: CardCategory):
        if payload.code not in org.category_titles:
            await event.ack(notification="Нет такой категории")
            return
        async with db.session() as s, s.begin():
            req = await repo.get_request(s, org.org_id, payload.id)
            if req is None:
                await event.ack(notification="Заявка не найдена")
                return
            changed = req.category != payload.code
            await repo.set_category(s, req, payload.code)
        await event.edit(text=render_card(org, req), attachments=card_keyboard(req))
        await sync_cards(event.bot, db, org, req, skip_mid=_pressed_mid(event))
        if changed and Status(req.status) == Status.NEW:
            # Заявка сменила направление — показываем её специалистам нового направления.
            await send_to_specialists(event.bot, db, org, req, members)

    return router
