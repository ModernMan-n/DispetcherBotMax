"""Проверка, что пользователь состоит в чате «Сотрудники» — защита от посторонних.

Бот должен быть администратором этого чата: только так MAX отдаёт список участников.
Результат проверки кэшируется ненадолго; при выходе или удалении из чата бот получает
событие user_removed и сбрасывает доступ сразу.
"""

from __future__ import annotations

import logging
import time

from maxapi import Bot

log = logging.getLogger(__name__)

MEMBER_TTL = 10 * 60  # участник: перепроверяем раз в 10 минут
NON_MEMBER_TTL = 60  # не участник или ошибка: только что вступивший ждёт не больше минуты


class MembershipChecker:
    def __init__(self, chat_id: int | None, *, clock=time.monotonic) -> None:
        self.chat_id = chat_id
        self._clock = clock
        self._cache: dict[int, tuple[bool, float]] = {}

    @property
    def enabled(self) -> bool:
        return self.chat_id is not None

    async def is_member(self, bot: Bot, user_id: int) -> bool:
        if not self.enabled:
            return True
        cached = self._cache.get(user_id)
        if cached is not None and cached[1] > self._clock():
            return cached[0]
        try:
            member = await bot.get_chat_member(chat_id=self.chat_id, user_id=user_id)
            ok = member is not None
        except Exception:
            # Проверка безопасности: если MAX не ответил — не пускаем.
            log.warning("Не удалось проверить членство в чате сотрудников", exc_info=True)
            ok = False
        self._remember(user_id, ok)
        return ok

    def _remember(self, user_id: int, ok: bool) -> None:
        self._cache[user_id] = (ok, self._clock() + (MEMBER_TTL if ok else NON_MEMBER_TTL))

    def joined(self, user_id: int) -> None:
        self._remember(user_id, True)

    def left(self, user_id: int) -> None:
        self._remember(user_id, False)
