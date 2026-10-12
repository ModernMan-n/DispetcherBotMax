"""Состояния FSM, payload кнопок и клавиатуры."""

from __future__ import annotations

from maxapi.context import State, StatesGroup
from maxapi.enums.intent import Intent
from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from app.config import OrgConfig


class NewRequest(StatesGroup):
    building = State()
    room = State()
    description = State()
    confirm = State()


class Action(CallbackPayload, prefix="a"):
    name: str  # new | my | send | cancel


class BuildingChoice(CallbackPayload, prefix="b"):
    code: str


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Новая заявка", payload=Action(name="new").pack(), intent=Intent.POSITIVE))
    kb.row(CallbackButton(text="Мои заявки", payload=Action(name="my").pack()))
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
