"""Метрики для дашборда руководства.

Два вида чисел:
- «сейчас» — снимок очереди (новые, в работе, просроченные) независимо от периода;
- «за период» — потоки за последние N дней (поступило, закрыто, время реакции и выполнения, исполнители)
  и их изменение к предыдущим N дням.

Персональные данные заявителей сюда не попадают: руководству они не нужны.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OrgConfig
from app.formatting import format_duration
from app.models import ACTIVE_STATUSES, Request, Status, utcnow


@dataclass
class _Durations:
    values: list[float]  # в минутах

    @property
    def avg(self) -> float | None:
        return round(mean(self.values), 1) if self.values else None


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60


def _human(minutes: float | None) -> str | None:
    return None if minutes is None else format_duration(timedelta(minutes=minutes))


def _change_pct(current: float | None, previous: float | None) -> float | None:
    """Изменение к прошлому периоду в процентах. Не с чем сравнить — None."""
    if current is None or not previous:
        return None
    return round(100 * (current - previous) / previous, 1)


def _flow(rows: list[Request], lo: datetime, hi: datetime, sla: dict[str, timedelta]) -> dict:
    """Потоки заявок в окне [lo, hi]."""

    def inside(dt: datetime | None) -> bool:
        return dt is not None and lo <= dt <= hi

    created = [r for r in rows if inside(r.created_at)]
    done = [r for r in rows if r.status == Status.DONE and inside(r.closed_at)]
    taken = [r for r in rows if inside(r.taken_at)]
    on_time = {r.id for r in done if r.closed_at - r.created_at <= sla.get(r.category, timedelta.max)}
    return {
        "created": created,
        "done": done,
        "on_time": on_time,
        "cancelled": sum(r.status == Status.CANCELLED and inside(r.closed_at) for r in rows),
        "avg_take_min": _Durations([_minutes(r.taken_at - r.created_at) for r in taken]).avg,
        "avg_close_min": _Durations([_minutes(r.closed_at - r.created_at) for r in done]).avg,
    }


async def collect(s: AsyncSession, org: OrgConfig, days: int, now: datetime | None = None) -> dict:
    now = now or utcnow()
    tz = ZoneInfo(org.timezone)
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    sla = {c.code: timedelta(hours=c.sla_hours) for c in org.categories}

    rows = list(
        await s.scalars(
            select(Request).where(
                Request.org_id == org.org_id,
                or_(
                    Request.created_at >= prev_start,
                    Request.closed_at >= prev_start,
                    Request.taken_at >= prev_start,
                    Request.status.in_(ACTIVE_STATUSES),
                ),
            )
        )
    )

    def is_overdue(r: Request) -> bool:
        limit = sla.get(r.category)
        return limit is not None and now - r.created_at > limit

    active = [r for r in rows if r.status in ACTIVE_STATUSES]
    cur = _flow(rows, start, now, sla)
    # Предыдущий период того же размера; граница start — только в текущем.
    prev = _flow(rows, prev_start, start - timedelta(microseconds=1), sla)
    done, on_time = cur["done"], cur["on_time"]

    # --- исполнители ---
    per_assignee: dict[int, dict] = defaultdict(
        lambda: {"name": "", "done": 0, "in_progress": 0, "close_minutes": [], "on_time": 0}
    )
    for r in done:
        if r.assignee is None:
            continue
        a = per_assignee[r.assignee.id]
        a["name"] = r.assignee.name
        a["done"] += 1
        a["close_minutes"].append(_minutes(r.closed_at - r.created_at))
        a["on_time"] += r.id in on_time
    for r in active:
        if r.status == Status.IN_PROGRESS and r.assignee is not None:
            a = per_assignee[r.assignee.id]
            a["name"] = r.assignee.name
            a["in_progress"] += 1
    assignees = []
    for a in per_assignee.values():
        avg = _Durations(a["close_minutes"]).avg
        assignees.append({
            "name": a["name"],
            "done": a["done"],
            "in_progress": a["in_progress"],
            "avg_close_min": avg,
            "avg_close": _human(avg),
            "on_time_pct": round(100 * a["on_time"] / a["done"]) if a["done"] else None,
        })
    assignees.sort(key=lambda x: (-x["done"], x["name"]))

    # --- просроченные сейчас (без данных заявителя) ---
    overdue_list = sorted(
        (
            {
                "id": r.id,
                "category": org.category_titles.get(r.category, r.category),
                "place": f"{org.building_titles.get(r.building, r.building)}, каб. {r.room}",
                "description": r.description,
                "status": r.status,
                "assignee": r.assignee.name if r.assignee else None,
                "age": format_duration(now - r.created_at),
                "over_by": format_duration(now - r.created_at - sla[r.category]),
                "age_min": _minutes(now - r.created_at),
            }
            for r in active
            if is_overdue(r)
        ),
        key=lambda x: -x["age_min"],
    )

    return {
        "org_name": org.name,
        "days": days,
        "generated_at": now.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        "now": {
            "new": sum(r.status == Status.NEW for r in active),
            "in_progress": sum(r.status == Status.IN_PROGRESS for r in active),
            "overdue": len(overdue_list),
        },
        "period": {
            "created": len(cur["created"]),
            "done": len(done),
            "cancelled": cur["cancelled"],
            "avg_take_min": cur["avg_take_min"],
            "avg_take": _human(cur["avg_take_min"]),
            "avg_close_min": cur["avg_close_min"],
            "avg_close": _human(cur["avg_close_min"]),
            "on_time_pct": round(100 * len(on_time) / len(done)) if done else None,
        },
        # Изменение к предыдущим N дням, %. Для времени рост — это плохо, интерпретирует страница.
        "change": {
            "created": _change_pct(len(cur["created"]), len(prev["created"])),
            "done": _change_pct(len(done), len(prev["done"])),
            "avg_take": _change_pct(cur["avg_take_min"], prev["avg_take_min"]),
            "avg_close": _change_pct(cur["avg_close_min"], prev["avg_close_min"]),
        },
        "assignees": assignees,
        "overdue": overdue_list,
    }
