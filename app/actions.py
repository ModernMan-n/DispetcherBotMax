"""Действия с заявками из веб-интерфейса — с теми же последствиями в MAX, что и в боте.

После изменения обновляются все карточки заявки (чат диспетчерской, лички специалистов),
заявитель получает уведомление о смене статуса.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from maxapi import Bot

from app import repo, staff
from app.bot.dispatch import _send_card, notify_applicant, send_to_specialists, sync_cards
from app.config import OrgConfig
from app.db import Database
from app.membership import MembershipChecker
from app.models import ACTIVE_STATUSES, TRANSITIONS, Assignee, Request, Status, StatusLog, utcnow

log = logging.getLogger(__name__)


class ActionError(Exception):
    """Сообщение для пользователя — в тексте исключения."""


@dataclass
class Ctx:
    bot: Bot | None
    db: Database
    org: OrgConfig
    members: MembershipChecker


STATUS_BY_ACTION = {
    "take": Status.IN_PROGRESS,
    "done": Status.DONE,
    "release": Status.NEW,
    "cancel": Status.CANCELLED,
}


async def _after_change(ctx: Ctx, req: Request, *, notify: bool) -> None:
    if ctx.bot is None:
        log.warning("Бот не настроен — карточки заявки №%s в MAX не обновлены", req.id)
        return
    await sync_cards(ctx.bot, ctx.db, ctx.org, req, skip_mid=None)
    if notify:
        await notify_applicant(ctx.bot, ctx.org, req)


async def _load(s, ctx: Ctx, request_id: int) -> Request:
    req = await repo.get_request(s, ctx.org.org_id, request_id)
    if req is None:
        raise ActionError("Заявка не найдена")
    return req


async def change_status(ctx: Ctx, request_id: int, action: str, actor: Assignee) -> Request:
    status = STATUS_BY_ACTION.get(action)
    if status is None:
        raise ActionError("Неизвестное действие")
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        if status not in TRANSITIONS[Status(req.status)]:
            raise ActionError("Это действие сейчас недоступно: заявку уже изменили")
        try:
            await repo.set_status(
                s, req, status, changed_by=actor.max_id,
                assignee_id=actor.id if status == Status.IN_PROGRESS else None,
            )
        except repo.TransitionError as e:
            raise ActionError(str(e)) from e
        await s.refresh(req, ["assignee"])
    log.info("Веб: заявка №%s → %s (сотрудник %s)", req.id, status, actor.id)
    await _after_change(ctx, req, notify=True)
    return req


async def set_category(ctx: Ctx, request_id: int, category: str, actor: Assignee) -> Request:
    if category not in ctx.org.category_titles:
        raise ActionError("Нет такой категории")
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        changed = req.category != category
        await repo.set_category(s, req, category)
    await _after_change(ctx, req, notify=False)
    if changed and Status(req.status) == Status.NEW and ctx.bot is not None:
        await send_to_specialists(ctx.bot, ctx.db, ctx.org, req, ctx.members)
    return req


async def assign(ctx: Ctx, request_id: int, assignee_id: int, actor: Assignee) -> Request:
    """Администратор назначает исполнителя: новая заявка уходит в работу, у взятой меняется исполнитель."""
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        target = await staff.get_specialist_by_id(s, ctx.org.org_id, assignee_id)
        if target is None or not target.active or not target.works_requests:
            raise ActionError("Этому сотруднику нельзя назначить заявку")
        status = Status(req.status)
        if status not in ACTIVE_STATUSES:
            raise ActionError("Заявка уже закрыта")
        if status == Status.NEW:
            try:
                await repo.set_status(s, req, Status.IN_PROGRESS, changed_by=actor.max_id, assignee_id=target.id)
            except repo.TransitionError as e:
                raise ActionError(str(e)) from e
        else:
            if req.assignee_id == target.id:
                raise ActionError("Заявка уже у этого исполнителя")
            req.assignee_id = target.id
            req.taken_at = utcnow()
            req.log.append(StatusLog(status=Status.IN_PROGRESS, changed_by=actor.max_id))
        await s.refresh(req, ["assignee"])
        target_max_id = target.max_id
    log.info("Веб: заявка №%s назначена сотруднику %s (назначил %s)", req.id, assignee_id, actor.id)
    await _after_change(ctx, req, notify=status == Status.NEW)
    if ctx.bot is not None and target_max_id is not None:
        try:
            await ctx.bot.send_message(user_id=target_max_id, text=f"Вам назначена заявка №{req.id}.")
        except Exception:
            log.exception("Не удалось уведомить исполнителя о назначении заявки №%s", req.id)
        await _send_card(ctx.bot, ctx.db, ctx.org, req, user_id=target_max_id)
    return req


async def edit_request(ctx: Ctx, request_id: int, fields: dict, actor: Assignee) -> Request:
    """Администратор правит корпус, кабинет или описание."""
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        if "building" in fields:
            if fields["building"] not in ctx.org.building_titles:
                raise ActionError("Нет такого корпуса")
            req.building = fields["building"]
        if "room" in fields:
            room = fields["room"].strip()
            if not room or len(room) > 20:
                raise ActionError("Кабинет: от 1 до 20 символов")
            req.room = room
        if "description" in fields:
            text = fields["description"].strip()
            if len(text) < 5:
                raise ActionError("Описание слишком короткое")
            req.description = text[:1000]
    log.info("Веб: заявка №%s отредактирована (сотрудник %s): %s", req.id, actor.id, sorted(fields))
    await _after_change(ctx, req, notify=False)
    return req
