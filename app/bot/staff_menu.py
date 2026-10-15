"""Специалист в боте: вход по номеру телефона, линия, списки активных заявок."""

from __future__ import annotations

import logging

from maxapi import F, Router
from maxapi.context import BaseContext
from maxapi.filters.command import Command
from maxapi.filters.contact import Contact
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types.attachments.contact import Contact as ContactAttachment

from app import staff
from app.bot import ui
from app.bot.handlers import InDialog, close_buttons
from app.bot.ui import StaffAction
from app.config import OrgConfig
from app.db import Database
from app.formatting import format_duration
from app.bot.dispatch import PRIORITY_MARKERS
from app.models import Assignee, Priority, Request, Status, utcnow

log = logging.getLogger(__name__)

# Параметр ссылки-приглашения для сотрудников: https://max.ru/<бот>?start=staff
STAFF_START_PAYLOAD = "staff"

STATUS_SHORT = {Status.NEW: "новая", Status.IN_PROGRESS: "в работе"}


def status_text(org: OrgConfig, a: Assignee) -> str:
    # В порядке категорий из конфига, а не как лежат в БД
    directions = ", ".join(c.title for c in org.categories if c.code in a.direction_codes) or "не назначены"
    if a.on_duty:
        duty = "🟢 На линии — новые заявки по вашим направлениям приходят сюда."
    else:
        duty = "⚪ Не на линии — уведомления о новых заявках не приходят."
    return f"Специалист: {a.name}\nНаправления: {directions}\n{duty}"


def request_list_text(org: OrgConfig, title: str, items: list[Request]) -> str:
    if not items:
        return f"{title}\nАктивных заявок нет."
    now = utcnow()
    lines = [f"{title} ({len(items)}):"]
    for r in items:
        place = f"{org.building_titles.get(r.building, r.building)}, каб. {r.room}"
        marker = PRIORITY_MARKERS.get(Priority(r.priority), "")
        overdue = " · ⚠️ просрочена" if r.is_overdue(now) else ""
        lines.append(
            f"{marker}№{r.id} · {org.category_titles.get(r.category, r.category)} · {place} · "
            f"{STATUS_SHORT[Status(r.status)]} · {format_duration(now - r.created_at)}{overdue}"
        )
    lines.append("\nНажмите номер, чтобы открыть заявку.")
    return "\n".join(lines)


def build_staff_router(org: OrgConfig, db: Database) -> Router:
    router = Router(router_id="staff")
    router.filter(InDialog())

    async def show_status(answer, a: Assignee) -> None:
        if not a.works_requests:
            # Руководство: в боте только подаёт заявки как все, аналитика — в веб-интерфейсе
            await answer(
                text=f"Вы вошли как {a.name} (руководство). Аналитика и заявки — в веб-интерфейсе, "
                     f"код для входа придёт сюда.",
                attachments=[ui.main_menu()],
            )
            return
        await answer(text=status_text(org, a), attachments=[ui.main_menu(a)])

    async def staff_entry(user_id: int, answer, context: BaseContext) -> None:
        """Вход сотрудника: уже привязан — показываем меню, нет — просим поделиться номером."""
        await context.clear()
        async with db.session() as s:
            a = await staff.get_staff_member(s, org.org_id, user_id)
        if a is not None:
            await show_status(answer, a)
            return
        await answer(
            text=(
                "Вход для сотрудников.\n"
                "Нажмите «Поделиться номером» — бот проверит, что вы есть в списке сотрудников. "
                "Номер нужен только для этого и обычным пользователям не запрашивается."
            ),
            attachments=[ui.share_contact_keyboard()],
        )

    @router.message_created(Command(["staff", "sotrudnik"]))
    async def on_staff_command(event: MessageCreated, context: BaseContext):
        await staff_entry(event.message.sender.user_id, event.message.answer, context)

    # Приглашение по ссылке https://max.ru/<бот>?start=staff (с сайта и из раздела «Сотрудники»)
    @router.bot_started(F.payload == STAFF_START_PAYLOAD)
    async def on_staff_link(event: BotStarted, context: BaseContext):
        async def answer(**kw):
            await event.bot.send_message(chat_id=event.chat_id, **kw)
        await staff_entry(event.user.user_id, answer, context)

    @router.message_created(F.message.body.text.regexp(rf"^/start\s+{STAFF_START_PAYLOAD}\b"))
    async def on_staff_start_text(event: MessageCreated, context: BaseContext):
        await staff_entry(event.message.sender.user_id, event.message.answer, context)

    @router.message_created(Contact())
    async def on_contact(event: MessageCreated, context: BaseContext, contact: ContactAttachment):
        sender = event.message.sender
        owner = contact.payload.max_info if contact.payload else None
        # Номер принимаем, только если это контакт самого отправителя (кнопка «Поделиться номером»),
        # а не пересланный чужой контакт.
        if owner is None or owner.user_id != sender.user_id:
            await event.message.answer(
                text="Нужен ваш собственный номер: нажмите кнопку «Поделиться номером».",
                attachments=[ui.share_contact_keyboard()],
            )
            return
        phone = contact.payload.vcf.phone
        reason = ""
        async with db.session() as s, s.begin():
            try:
                a = await staff.link_by_phone(s, org.org_id, phone or "", sender.user_id)
            except staff.StaffError as e:
                reason = str(e)
                a = None
        if a is None:
            log.info("Вход специалиста не удался (%s): %s", reason, staff.mask_phone(staff.normalize_phone(phone or "")))
            text = (
                "Этот номер уже привязан к другому аккаунту MAX. Обратитесь к администратору."
                if reason == "linked_to_other"
                else "Номер не найден среди сотрудников. Если вы специалист, обратитесь к администратору."
            )
            await event.message.answer(text=text)
            return
        await context.clear()
        log.info("Специалист %s вошёл в бот", a.id)
        await event.message.answer(text="Номер подтверждён, вы вошли как специалист.")
        await show_status(event.message.answer, a)

    async def require_specialist(event: MessageCallback) -> Assignee | None:
        async with db.session() as s:
            a = await staff.get_specialist(s, org.org_id, event.callback.user.user_id)
        if a is None:
            await event.ack(notification="Доступно только специалистам")
        return a

    @router.message_callback(StaffAction.filter(F.name.in_({"duty_on", "duty_off"})))
    async def on_duty(event: MessageCallback, payload: StaffAction):
        if await require_specialist(event) is None:
            return
        async with db.session() as s, s.begin():
            a = await staff.get_specialist(s, org.org_id, event.callback.user.user_id)
            a.on_duty = payload.name == "duty_on"
        log.info("Специалист %s: на линии = %s", a.id, a.on_duty)
        await event.edit(text=status_text(org, a), attachments=[ui.main_menu(a)])

    @router.message_callback(StaffAction.filter(F.name.in_({"mine", "all"})))
    async def on_list(event: MessageCallback, payload: StaffAction):
        a = await require_specialist(event)
        if a is None:
            return
        async with db.session() as s:
            if payload.name == "mine":
                items = await staff.list_active_requests(s, org.org_id, a.direction_codes)
                title = "Активные заявки ваших направлений"
            else:
                items = await staff.list_active_requests(s, org.org_id)
                title = "Все активные заявки"
        await close_buttons(event)
        attachments = [ui.request_list_keyboard([r.id for r in items])] if items else []
        await event.send(text=request_list_text(org, title, items), attachments=attachments)
        await event.send(text=status_text(org, a), attachments=[ui.main_menu(a)])

    return router

