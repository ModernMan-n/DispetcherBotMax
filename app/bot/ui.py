"""Состояния FSM, payload кнопок и клавиатуры."""

from __future__ import annotations

from maxapi.context import State, StatesGroup
from maxapi.enums.intent import Intent
from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.request_contact import RequestContactButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from app.bot.dispatch import Card
from app.config import OrgConfig
from app.models import Assignee, Role


class NewRequest(StatesGroup):
    building = State()
    room = State()
    description = State()
    confirm = State()


class Action(CallbackPayload, prefix="a"):
    name: str  # new | my | send | cancel


class BuildingChoice(CallbackPayload, prefix="b"):
    code: str


class StaffAction(CallbackPayload, prefix="s"):
    name: str  # taken | mine | all | duty_on | duty_off


def main_menu(specialist: Assignee | None = None):
    kb = InlineKeyboardBuilder()
    if specialist is not None:
        kb.row(CallbackButton(text="Взятые мной", payload=StaffAction(name="taken").pack()))
        kb.row(CallbackButton(text="Заявки моих направлений", payload=StaffAction(name="mine").pack()))
        kb.row(CallbackButton(text="Все активные заявки", payload=StaffAction(name="all").pack()))
        if specialist.on_duty:
            kb.row(CallbackButton(text="Уйти с линии", payload=StaffAction(name="duty_off").pack()))
        else:
            kb.row(CallbackButton(
                text="Выйти на линию", payload=StaffAction(name="duty_on").pack(), intent=Intent.POSITIVE
            ))
        if specialist.role != Role.SPECIALIST:
            # Диспетчер и администратор могут и подавать заявки; специалист — только выполняет
            kb.row(
                CallbackButton(text="Новая заявка", payload=Action(name="new").pack()),
                CallbackButton(text="Мои заявки", payload=Action(name="my").pack()),
            )
    else:
        kb.row(CallbackButton(text="Новая заявка", payload=Action(name="new").pack(), intent=Intent.POSITIVE))
        kb.row(CallbackButton(text="Мои заявки", payload=Action(name="my").pack()))
    return kb.as_markup()


def share_contact_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(RequestContactButton(text="Поделиться номером"))
    kb.row(cancel_button())
    return kb.as_markup()


def request_list_keyboard(request_ids: list[int]):
    kb = InlineKeyboardBuilder()
    for rid in request_ids:
        kb.add(CallbackButton(text=f"№{rid}", payload=Card(act="open", id=rid).pack()))
    kb.adjust(4)
    return kb.as_markup()


def buildings_keyboard(org: OrgConfig):
    kb = InlineKeyboardBuilder()
    for b in org.buildings:
        kb.add(CallbackButton(text=b.title, payload=BuildingChoice(code=b.code).pack()))
    kb.adjust(2)
    kb.row(cancel_button())
    return kb.as_markup()


def cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(cancel_button())
    return kb.as_markup()


def confirm_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="Отправить", payload=Action(name="send").pack(), intent=Intent.POSITIVE),
        cancel_button(),
    )
    return kb.as_markup()


def cancel_button() -> CallbackButton:
    return CallbackButton(text="Отменить", payload=Action(name="cancel").pack(), intent=Intent.NEGATIVE)
