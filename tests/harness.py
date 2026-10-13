"""Подставной MAX для сквозных тестов бота: события на вход, запись исходящих сообщений."""

from __future__ import annotations

import itertools

from maxapi import Bot
from maxapi.methods.types.getted_updates import get_update_model

APPLICANT = {"user_id": 100, "first_name": "Иван", "last_name": "Тестов", "is_bot": False}
WORKER = {"user_id": 200, "first_name": "Пётр", "last_name": "Мастер", "is_bot": False}
WORKER2 = {"user_id": 201, "first_name": "Анна", "is_bot": False}
BOT_USER = {"user_id": 1, "first_name": "Бот", "is_bot": True}
DIALOG_ID = 500
DISPATCH_CHAT_ID = -777

_seq = itertools.count(1)


def buttons_of(attachments) -> list[str]:
    for att in attachments or []:
        if hasattr(att, "payload") and hasattr(att.payload, "buttons"):
            return [b.payload for row in att.payload.buttons for b in row]
    return []


class Harness:
    def __init__(self, dp, bot: Bot) -> None:
        self.dp, self.bot = dp, bot
        self.sent: list[dict] = []  # исходящие сообщения
        self.callbacks: list[dict] = []  # ответы на нажатия (правки сообщения и всплывашки)
        self.edits: list[dict] = []  # bot.edit_message — обновление карточек в других местах
        # Чат «Сотрудники»: кто в нём состоит, сколько раз бот спросил MAX, «упал» ли MAX
        self.staff_members: set[int] = {APPLICANT["user_id"], WORKER["user_id"], WORKER2["user_id"]}
        self.member_checks = 0
        self.members_api_down = False

    # --- что ответил бот ---

    def in_dialog(self) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == DIALOG_ID]

    def in_chat(self) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == DISPATCH_CHAT_ID]

    def to_user(self, user_id: int) -> list[dict]:
        return [m for m in self.sent if m["user_id"] == user_id]

    def message(self, mid: str) -> dict:
        """Текущее состояние отправленного сообщения с учётом правок."""
        msg = dict(next(m for m in self.sent if m["mid"] == mid))
        for e in self.edits:
            if e["message_id"] == mid:
                msg.update(text=e["text"], attachments=e["attachments"])
        return msg

    @property
    def last_text(self) -> str:
        return self.in_dialog()[-1]["text"]

    def last_buttons(self) -> list[str]:
        return buttons_of(self.in_dialog()[-1]["attachments"])

    @property
    def last_edit(self):
        return self.callbacks[-1]["message"]

    # --- что делает пользователь ---

    async def _feed(self, raw: dict) -> None:
        await self.dp.handle(await get_update_model(raw, self.bot))

    async def text(self, text: str, *, user=APPLICANT, chat_id=DIALOG_ID, chat_type="dialog") -> None:
        n = next(_seq)
        await self._feed({
            "update_type": "message_created",
            "timestamp": n,
            "message": {
                "sender": user,
                "recipient": {"chat_id": chat_id, "chat_type": chat_type},
                "timestamp": n,
                "body": {"mid": f"m{n}", "seq": n, "text": text},
            },
        })

    async def contact(self, *, user, owner, phone: str, chat_id=DIALOG_ID) -> None:
        """Пользователь user отправил контакт, принадлежащий owner (себя — кнопкой «Поделиться номером»)."""
        n = next(_seq)
        vcf = f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{owner['first_name']}\r\nTEL;TYPE=cell:{phone}\r\nEND:VCARD"
        await self._feed({
            "update_type": "message_created",
            "timestamp": n,
            "message": {
                "sender": user,
                "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
                "timestamp": n,
                "body": {"mid": f"m{n}", "seq": n, "text": None, "attachments": [
                    {"type": "contact", "payload": {"vcf_info": vcf, "max_info": owner}},
                ]},
            },
        })

    async def press(
        self, payload: str, *, user=APPLICANT, chat_id=DIALOG_ID, chat_type="dialog", mid: str | None = None
    ) -> None:
        n = next(_seq)
        await self._feed({
            "update_type": "message_callback",
            "timestamp": n,
            "callback": {"timestamp": n, "callback_id": f"cb{n}", "payload": payload, "user": user},
            "message": {
                "sender": BOT_USER,
                "recipient": {"chat_id": chat_id, "chat_type": chat_type},
                "timestamp": n,
                "body": {"mid": mid or f"b{n}", "seq": n, "text": "Сообщение с кнопками"},
            },
        })

    async def chat_event(self, update_type: str, *, chat_id: int, user: dict) -> None:
        """user_added / user_removed в групповом чате."""
        n = next(_seq)
        await self._feed({"update_type": update_type, "timestamp": n, "chat_id": chat_id,
                          "user": user, "is_channel": False})

    async def press_in_chat(self, payload: str, user=WORKER, mid: str | None = None) -> None:
        await self.press(payload, user=user, chat_id=DISPATCH_CHAT_ID, chat_type="chat", mid=mid)

    async def submit_request(self, description="Не работает проектор, не включается") -> None:
        await self.press("a|new")
        await self.press("b|2")
        await self.text("214")
        await self.text(description)
        await self.press("a|send")


