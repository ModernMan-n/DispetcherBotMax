"""Форматирование для людей: длительности и т.п."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def format_duration(delta: timedelta) -> str:
    """«34 мин», «5 ч 12 мин», «2 д 3 ч»."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} д"] if days else []
    if hours:
        parts.append(f"{hours} ч")
    if (mins and not days) or not parts:
        parts.append(f"{mins} мин")
    return " ".join(parts)


def fmt_local(dt: datetime | None, tz: str) -> str:
    """«15.10 18:00» в часовом поясе организации."""
    return "—" if dt is None else dt.astimezone(ZoneInfo(tz)).strftime("%d.%m %H:%M")
