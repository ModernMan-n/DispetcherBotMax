"""Запуск бота: webhook на сервере и защита polling от поломки серверной подписки."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.__main__ import run_polling, run_webhook

URL = "https://dispatch.example.ru/max-webhook"


class FakeBot:
    def __init__(self, urls=()):
        self.urls = list(urls)
        self.subscribed = []
        self.unsubscribed = []

    async def get_subscriptions(self):
        return SimpleNamespace(subscriptions=[SimpleNamespace(url=u) for u in self.urls])

    async def subscribe_webhook(self, url, secret=None, update_types=None):
        self.subscribed.append((url, secret))

    async def unsubscribe_webhook(self, url):
        self.unsubscribed.append(url)


class FakeDp:
    def __init__(self):
        self.webhook = None
        self.polling = False

    async def handle_webhook(self, bot, **kw):
        self.webhook = kw

    async def start_polling(self, bot):
        self.polling = True


def settings(url=URL, secret="s3cret-value"):
    return SimpleNamespace(webhook_url=url, webhook_secret=secret)


async def test_webhook_subscribes_and_drops_old_urls():
    bot, dp = FakeBot(urls=["https://old.example.ru/max-webhook", URL]), FakeDp()
    await run_webhook(dp, bot, settings())
    assert bot.unsubscribed == ["https://old.example.ru/max-webhook"]
    assert bot.subscribed == [(URL, "s3cret-value")]
    assert dp.webhook == {"host": "0.0.0.0", "port": 8080, "path": "/max-webhook", "secret": "s3cret-value"}


@pytest.mark.parametrize("url", [None, "http://dispatch.example.ru/max-webhook", "https://dispatch.example.ru/other"])
async def test_webhook_rejects_bad_url(url):
    with pytest.raises(SystemExit, match="WEBHOOK_URL"):
        await run_webhook(FakeDp(), FakeBot(), settings(url=url))


@pytest.mark.parametrize("secret", [None, "abc"])
async def test_webhook_requires_secret(secret):
    with pytest.raises(SystemExit, match="WEBHOOK_SECRET"):
        await run_webhook(FakeDp(), FakeBot(), settings(secret=secret))


async def test_polling_refuses_when_server_webhook_exists():
    bot, dp = FakeBot(urls=[URL]), FakeDp()
    with pytest.raises(SystemExit, match="webhook"):
        await run_polling(dp, bot)
    assert not dp.polling and bot.unsubscribed == []  # серверную подписку не тронули


async def test_polling_starts_without_webhook():
    dp = FakeDp()
    await run_polling(dp, FakeBot())
    assert dp.polling
