"""Действия с заявками из веб-интерфейса — с теми же последствиями в MAX, что и в боте.

После изменения обновляются все карточки заявки (чат диспетчерской, лички специалистов),
заявитель получает уведомление о смене статуса. Правки, срок, приоритет, назначение
записываются в историю заявки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from maxapi import Bot

from app import repo, staff
from app.bot.dispatch import (
    CARD_FORMAT,
    team_text,
    esc,
    notify_applicant,
    profile_link,
    send_card,
    send_to_specialists,
    sync_cards,
    tell_applicant,
)
from app.config import OrgConfig
from app.db import Database
from app.formatting import fmt_local
from app.membership import MembershipChecker
from app.models import ACTIVE_STATUSES, TRANSITIONS, Assignee, Priority, Request, Status, utcnow

log = logging.getLogger(__name__)

PRIORITY_TITLES = {
    Priority.URGENT: "срочный",
    Priority.HIGH: "высокий",
    Priority.NORMAL: "обычный",
    Priority.LOW: "низкий",
}


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


async def _after_change(ctx: Ctx, req: Request, *, notify: bool, actor: Assignee | None = None) -> None:
    """Обновить карточки в MAX; notify — сообщить заявителю о смене статуса."""
    if ctx.bot is None:
        log.warning("Бот не настроен — карточки заявки №%s в MAX не обновлены", req.id)
        return
    await sync_cards(ctx.bot, ctx.db, ctx.org, req, skip_mid=None)
    if notify:
        await notify_applicant(ctx.bot, ctx.org, req, actor_max_id=actor.max_id if actor else None)


async def _tell_applicant(ctx: Ctx, req: Request, key: str, actor: Assignee, **values) -> None:
    if ctx.bot is not None:
        await tell_applicant(ctx.bot, ctx.org, req, key, actor_max_id=actor.max_id, **values)


async def _tell(ctx: Ctx, max_id: int | None, text: str) -> None:
    if ctx.bot is None or max_id is None:
        return
    try:
        await ctx.bot.send_message(user_id=max_id, text=text)
    except Exception:
        log.exception("Не удалось отправить сообщение сотруднику")


async def _load(s, ctx: Ctx, request_id: int) -> Request:
    req = await repo.get_request(s, ctx.org.org_id, request_id)
    if req is None:
        raise ActionError("Заявка не найдена")
    return req


def _require_active(req: Request) -> None:
    if Status(req.status) not in ACTIVE_STATUSES:
        raise ActionError("Заявка уже закрыта")


async def _event(s, req: Request, kind: str, text: str, actor: Assignee) -> None:
    await repo.log_event(s, req, kind, text, actor_id=actor.id, actor_name=actor.name)


async def change_status(ctx: Ctx, request_id: int, action: str, actor: Assignee) -> Request:
    status = STATUS_BY_ACTION.get(action)
    if status is None:
        raise ActionError("Неизвестное действие")
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        if status not in TRANSITIONS[Status(req.status)]:
            raise ActionError("Это действие сейчас недоступно: заявку уже изменили")
        # Один из нескольких исполнителей возвращает заявку — снимаем только его
        left = status == Status.NEW and await repo.leave_team(s, req, actor)
        if not left:
            try:
                await repo.set_status(
                    s, req, status, changed_by=actor.max_id,
                    assignee_id=actor.id if status == Status.IN_PROGRESS else None,
                )
            except repo.TransitionError as e:
                raise ActionError(str(e)) from e
        await s.refresh(req, ["assignee", "team"])
    log.info("Веб: заявка №%s → %s (сотрудник %s%s)", req.id, status, actor.id, ", снял себя" if left else "")
    await _after_change(ctx, req, notify=not left, actor=actor)
    if left:
        await _tell_applicant(ctx, req, "notify_reassigned", actor, assignees=team_text(req))
    return req


async def set_category(ctx: Ctx, request_id: int, category: str, actor: Assignee) -> Request:
    titles = ctx.org.category_titles
    if category not in titles or category not in {c.code for c in ctx.org.active_categories}:
        raise ActionError("Нет такой категории")
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        old = req.category
        if old == category:
            return req
        await repo.set_category(s, req, category, ctx.org.category_sla.get(category))
        await _event(s, req, "category", f"Категория: {titles.get(old, old)} → {titles[category]}", actor)
    await _after_change(ctx, req, notify=False)
    if Status(req.status) == Status.NEW and ctx.bot is not None:
        await send_to_specialists(ctx.bot, ctx.db, ctx.org, req, ctx.members)
    return req


async def set_priority(ctx: Ctx, request_id: int, priority: str, actor: Assignee) -> Request:
    try:
        new = Priority(priority)
    except ValueError as e:
        raise ActionError("Нет такого приоритета") from e
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        _require_active(req)
        old = Priority(req.priority)
        if old == new:
            return req
        req.priority = new
        await _event(s, req, "priority", f"Приоритет: {PRIORITY_TITLES[old]} → {PRIORITY_TITLES[new]}", actor)
        team_max = [a.max_id for a in req.team if a.max_id and a.max_id != actor.max_id]
    log.info("Веб: заявка №%s, приоритет %s → %s (сотрудник %s)", req.id, old, new, actor.id)
    await _after_change(ctx, req, notify=False)
    await _tell_applicant(ctx, req, "notify_priority", actor, priority=PRIORITY_TITLES[new])
    if new in (Priority.URGENT, Priority.HIGH):
        for max_id in team_max:
            await _tell(ctx, max_id, f"Заявке №{req.id} поставлен {PRIORITY_TITLES[new]} приоритет.")
    return req


async def set_due(
    ctx: Ctx, request_id: int, actor: Assignee, *,
    due_at: datetime | None = None, shift_hours: int | None = None, reset: bool = False, comment: str = "",
) -> Request:
    """Срок выполнения: перенести на дату, сдвинуть на N часов или вернуть к нормативу категории."""
    now = utcnow()
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        _require_active(req)
        sla = ctx.org.category_sla.get(req.category, 48)
        normative = req.created_at + timedelta(hours=sla)
        old = req.due_at or normative
        if reset:
            new, manual = normative, False
        elif shift_hours is not None:
            new, manual = max(old, now) + timedelta(hours=shift_hours), True
        elif due_at is not None:
            new, manual = due_at, True
        else:
            raise ActionError("Укажите новый срок")
        if new <= now and not reset:
            raise ActionError("Новый срок уже прошёл")
        req.due_at, req.due_manual = new, manual
        tz = ctx.org.timezone
        text = f"Срок: {fmt_local(old, tz)} → {fmt_local(new, tz)}"
        if reset:
            text += " (по нормативу категории)"
        if comment.strip():
            text += f". Причина: {comment.strip()[:300]}"
        await _event(s, req, "due", text, actor)
        team_max = [a.max_id for a in req.team if a.max_id and a.max_id != actor.max_id]
    log.info("Веб: заявка №%s, срок → %s (сотрудник %s)", req.id, new, actor.id)
    await _after_change(ctx, req, notify=False)
    reason = comment.strip()[:300]
    # Причину видят и заявитель, и исполнитель (уведомление заявителю — в HTML, поэтому экранируем)
    if reason:
        await _tell_applicant(ctx, req, "notify_due_reason", actor, due=fmt_local(new, ctx.org.timezone),
                              reason=esc(reason))
    else:
        await _tell_applicant(ctx, req, "notify_due", actor, due=fmt_local(new, ctx.org.timezone))
    text = f"Срок по заявке №{req.id} изменён: до {fmt_local(new, ctx.org.timezone)}."
    for max_id in team_max:
        await _tell(ctx, max_id, f"{text} Причина: {reason}" if reason else text)
    return req


async def assign(ctx: Ctx, request_id: int, assignee_ids: list[int], actor: Assignee) -> Request:
    """Назначить исполнителей (одного или нескольких). Новая заявка уходит в работу.

    Состав заменяется целиком: добавленные получают уведомление и карточку, снятые — уведомление.
    """
    ids = list(dict.fromkeys(assignee_ids))
    if not ids:
        raise ActionError("Выберите хотя бы одного исполнителя — или верните заявку в очередь")
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        _require_active(req)
        targets = []
        for aid in ids:
            t = await staff.get_specialist_by_id(s, ctx.org.org_id, aid)
            if t is None or not t.active or not t.works_requests:
                raise ActionError("Одному из выбранных сотрудников нельзя назначить заявку")
            targets.append(t)
        status = Status(req.status)
        before = {a.id: a for a in req.team}
        if set(before) == set(ids):
            raise ActionError("Состав исполнителей не изменился")
        if status == Status.NEW:
            try:
                await repo.set_status(s, req, Status.IN_PROGRESS, changed_by=actor.max_id, assignee_id=ids[0])
            except repo.TransitionError as e:
                raise ActionError(str(e)) from e
        await repo.set_team(s, req, ids)
        added = [t for t in targets if t.id not in before]
        removed = [a for aid, a in before.items() if aid not in ids]
        changes = []
        if added:
            changes.append("назначены: " + ", ".join(a.name for a in added))
        if removed:
            changes.append("сняты: " + ", ".join(a.name for a in removed))
        await _event(s, req, "assign", "Исполнители — " + "; ".join(changes), actor)
        added_max = [(a.max_id, [o.name for o in targets if o.id != a.id]) for a in added if a.max_id]
        removed_max = [a.max_id for a in removed if a.max_id]
    log.info("Веб: заявка №%s, исполнители %s (назначил %s)", req.id, ids, actor.id)
    await _after_change(ctx, req, notify=status == Status.NEW, actor=actor)
    if status != Status.NEW:
        await _tell_applicant(ctx, req, "notify_reassigned", actor, assignees=team_text(req))
    for max_id, others in added_max:
        if max_id == actor.max_id:
            continue
        together = f" Вместе с вами: {', '.join(others)}." if others else ""
        await _tell(ctx, max_id, f"Вам назначена заявка №{req.id}.{together}")
        if ctx.bot is not None:
            await send_card(ctx.bot, ctx.db, ctx.org, req, user_id=max_id)
    for max_id in removed_max:
        if max_id != actor.max_id:
            await _tell(ctx, max_id, f"Вас сняли с заявки №{req.id}.")
    return req


async def edit_request(ctx: Ctx, request_id: int, fields: dict, actor: Assignee) -> Request:
    """Диспетчер правит корпус, кабинет или описание."""
    org = ctx.org
    async with ctx.db.session() as s, s.begin():
        req = await _load(s, ctx, request_id)
        changes = []
        if "building" in fields and fields["building"] != req.building:
            if fields["building"] not in {b.code for b in org.active_buildings}:
                raise ActionError("Нет такого корпуса")
            changes.append(f"корпус: {org.building_titles.get(req.building, req.building)} → "
                           f"{org.building_titles[fields['building']]}")
            req.building = fields["building"]
        if "room" in fields:
            room = fields["room"].strip()
            if not room or len(room) > 20:
                raise ActionError("Кабинет: от 1 до 20 символов")
            if room != req.room:
                changes.append(f"кабинет: {req.room} → {room}")
                req.room = room
        if "description" in fields:
            text = fields["description"].strip()
            if len(text) < 5:
                raise ActionError("Описание слишком короткое")
            if text[:1000] != req.description:
                changes.append("описание проблемы")
                req.description = text[:1000]
        if changes:
            await _event(s, req, "edit", "Изменено: " + "; ".join(changes), actor)
    log.info("Веб: заявка №%s отредактирована (сотрудник %s)", req.id, actor.id)
    if changes:
        await _after_change(ctx, req, notify=False)
    return req


async def contact_applicant(ctx: Ctx, request_id: int, actor: Assignee) -> None:
    """Прислать сотруднику в MAX ссылку на профиль заявителя (в браузере max:// часто не открывается)."""
    if ctx.bot is None:
        raise ActionError("Бот не настроен")
    if actor.max_id is None:
        raise ActionError("Ваш аккаунт MAX не привязан")
    async with ctx.db.session() as s:
        req = await _load(s, ctx, request_id)
    try:
        await ctx.bot.send_message(
            user_id=actor.max_id, format=CARD_FORMAT,
            text=f"Заявитель по заявке №{req.id}: {profile_link(req.applicant_name, req.applicant_max_id)}",
        )
    except Exception as e:
        log.exception("Не удалось прислать контакт заявителя по заявке №%s", req.id)
        raise ActionError("Не удалось отправить сообщение в MAX") from e
