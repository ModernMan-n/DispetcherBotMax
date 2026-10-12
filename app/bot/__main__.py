"""Запуск бота: python -m app.bot (локально через polling)."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import sys

from maxapi import Bot

from app.bot.setup import create_dispatcher
from app.config import ROOT, load_settings
from app.db import open_db


def acquire_single_instance_lock():
    """Две копии бота делят события и FSM, и бот отвечает дважды. Не даём запустить вторую."""
    lock_file = open(ROOT / "data" / "bot.lock", "w")  # noqa: SIM115 — держим открытым до выхода
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Бот уже запущен в другом процессе. Остановите его перед новым запуском.")
    return lock_file


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.bot_token:
        sys.exit("Не задан MAX_BOT_TOKEN (см. .env.example)")

    db = await open_db(settings.database_url, settings.org)
    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(settings.org, db)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    _lock = acquire_single_instance_lock()
    asyncio.run(main())
