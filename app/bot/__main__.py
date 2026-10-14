"""Запуск бота: python -m app.bot

BOT_MODE=polling (по умолчанию) — для разработки на своём компьютере.
BOT_MODE=webhook — на сервере: MAX присылает события на WEBHOOK_URL, бот слушает порт 8080.
"""

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

    if settings.org.staff_chat_id is None:
        logging.warning("STAFF_CHAT_ID не задан — ботом может пользоваться любой пользователь MAX")
    else:
        logging.info("Доступ только участникам чата сотрудников")
    db = await open_db(settings.database_url, settings.org)
    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(settings.org, db)

    try:
        if settings.bot_mode == "webhook":
            await run_webhook(dp, bot, settings)
        else:
            await run_polling(dp, bot)
    finally:
        await db.close()


WEBHOOK_PATH = "/max-webhook"
WEBHOOK_PORT = 8080


async def run_webhook(dp, bot: Bot, settings) -> None:
    url, secret = settings.webhook_url, settings.webhook_secret
    if not url or not url.startswith("https://") or not url.endswith(WEBHOOK_PATH):
        sys.exit(f"WEBHOOK_URL должен быть вида https://ваш-домен{WEBHOOK_PATH}")
    if not secret or not 5 <= len(secret) <= 256:
        sys.exit("WEBHOOK_SECRET: от 5 до 256 символов (см. .env.example)")
    # Подписка ровно одна — на наш адрес. Старые адреса (например, прежний домен) снимаем.
    subs = await bot.get_subscriptions()
    for sub in subs.subscriptions or []:
        if sub.url != url:
            await bot.unsubscribe_webhook(sub.url)
            logging.info("Снята старая подписка webhook: %s", sub.url)
    await bot.subscribe_webhook(url=url, secret=secret)
    logging.info("Webhook: %s", url)
    await dp.handle_webhook(bot, host="0.0.0.0", port=WEBHOOK_PORT, path=WEBHOOK_PATH, secret=secret)


async def run_polling(dp, bot: Bot) -> None:
    subs = await bot.get_subscriptions()
    if subs.subscriptions:
        # Не удаляем подписку молча: скорее всего, это работающий сервер — он перестал бы получать события.
        sys.exit(
            "У бота есть webhook-подписка (похоже, он работает на сервере): "
            + ", ".join(s.url for s in subs.subscriptions)
            + "\nДля разработки заведите отдельного тестового бота с другим токеном."
        )
    await dp.start_polling(bot)


if __name__ == "__main__":
    _lock = acquire_single_instance_lock()
    asyncio.run(main())
