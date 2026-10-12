"""Загрузка настроек: секреты из окружения, специфика организации из YAML."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator

ROOT = Path(__file__).resolve().parent.parent


class Building(BaseModel):
    code: str
    title: str


class Category(BaseModel):
    code: str
    title: str
    sla_hours: int
    keywords: list[str] = []
    fallback: bool = False


class OrgConfig(BaseModel):
    org_id: str
    name: str
    # Групповой чат диспетчерской в MAX. ID чата бот пишет туда сам при добавлении.
    dispatcher_chat_id: int | None = None
    timezone: str = "Europe/Moscow"
    buildings: list[Building]
    categories: list[Category]
    texts: dict[str, str]

    @model_validator(mode="after")
    def _check(self) -> OrgConfig:
        if not self.buildings:
            raise ValueError("Нужен хотя бы один корпус")
        fallbacks = [c for c in self.categories if c.fallback]
        if len(fallbacks) != 1:
            raise ValueError("Ровно одна категория должна быть fallback: true")
        for field, items in (("buildings", self.buildings), ("categories", self.categories)):
            codes = [i.code for i in items]
            if len(codes) != len(set(codes)):
                raise ValueError(f"Повторяющиеся code в {field}")
        return self

    @cached_property
    def building_titles(self) -> dict[str, str]:
        return {b.code: b.title for b in self.buildings}

    @cached_property
    def category_titles(self) -> dict[str, str]:
        return {c.code: c.title for c in self.categories}

    @cached_property
    def fallback_category(self) -> Category:
        return next(c for c in self.categories if c.fallback)

    def text(self, key: str, **kwargs: object) -> str:
        return self.texts[key].format(org_name=self.name, **kwargs).strip()


def load_org_config(path: str | Path) -> OrgConfig:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    with path.open(encoding="utf-8") as f:
        return OrgConfig.model_validate(yaml.safe_load(f))


class Settings(BaseModel):
    bot_token: str | None
    database_url: str
    org: OrgConfig


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    org = load_org_config(os.getenv("ORG_CONFIG", "config/org.yaml"))
    if chat_id := os.getenv("DISPATCHER_CHAT_ID"):
        org = org.model_copy(update={"dispatcher_chat_id": int(chat_id)})
    return Settings(
        bot_token=os.getenv("MAX_BOT_TOKEN") or None,
        database_url=os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'data' / 'bot.db'}"),
        org=org,
    )
