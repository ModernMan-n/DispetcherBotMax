"""Метрики для дашборда руководства.

Два вида чисел:
- «сейчас» — снимок очереди (новые, в работе, просроченные) независимо от периода;
- «за период» — потоки за последние N дней (поступило, закрыто, время реакции и выполнения, исполнители).

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


async def collect(s: AsyncSession, org: OrgConfig, days: int, now: datetime | None = None) -> dict:
    now = now or utcnow()
    tz = ZoneInfo(org.timezone)
    start = now - timedelta(days=days)
    sla = {c.code: timedelta(hours=c.sla_hours) for c in org.categories}

    rows = list(
        await s.scalars(
            select(Request).where(
                Request.org_id == org.org_id,
                or_(
                    Request.created_at >= start,
                    Request.closed_at >= start,
                    Request.taken_at >= start,
                    Request.status.in_(ACTIVE_STATUSES),
                ),
            )
        )
    )

    def in_period(dt: datetime | None) -> bool:
        return dt is not None and start <= dt <= now

    def is_overdue(r: Request) -> bool:
        limit = sla.get(r.category)
        return limit is not None and now - r.created_at > limit

    active = [r for r in rows if r.status in ACTIVE_STATUSES]
    created = [r for r in rows if in_period(r.created_at)]
    done = [r for r in rows if r.status == Status.DONE and in_period(r.closed_at)]
    taken = [r for r in rows if in_period(r.taken_at)]

    to_take = _Durations([_minutes(r.taken_at - r.created_at) for r in taken])
    to_close = _Durations([_minutes(r.closed_at - r.created_at) for r in done])
    on_time = {r.id for r in done if r.closed_at - r.created_at <= sla.get(r.category, timedelta.max)}

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
            "created": len(created),
            "done": len(done),
            "cancelled": sum(r.status == Status.CANCELLED and in_period(r.closed_at) for r in rows),
            "avg_take_min": to_take.avg,
            "avg_take": _human(to_take.avg),
            "avg_close_min": to_close.avg,
            "avg_close": _human(to_close.avg),
            "on_time_pct": round(100 * len(on_time) / len(done)) if done else None,
        },
        "assignees": assignees,
        "overdue": overdue_list,
    }
