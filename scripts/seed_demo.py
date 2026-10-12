"""Заполняет отдельную демо-БД правдоподобными вымышленными заявками для дашборда.

    python -m scripts.seed_demo                 # data/demo.db, 300 заявок за 60 дней
    python -m scripts.seed_demo --count 500 --days 90

Реальных персональных данных здесь нет: заявители — «Тестовый заявитель N»
с отрицательными ID, исполнители — должности.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app import repo
from app.config import ROOT, load_org_config
from app.db import open_db
from app.models import Assignee, Status

LOCAL_TZ = ZoneInfo("Europe/Moscow")

DESCRIPTIONS = {
    "electric": [
        "Не работает розетка у доски",
        "Мигает свет в кабинете",
        "Перегорели две лампы над партами",
        "Искрит выключатель",
        "Нет света в коридоре у кабинета",
    ],
    "network": [
        "Нет интернета на компьютере преподавателя",
        "Не подключается Wi-Fi",
        "Очень медленный интернет, ничего не грузится",
        "Сетевой кабель оторван от розетки",
    ],
    "computers": [
        "Не включается проектор",
        "Принтер не печатает, пишет ошибку",
        "Компьютер зависает при запуске",
        "Не работает интерактивная доска",
        "Закончился картридж в МФУ",
        "Монитор не показывает изображение",
    ],
    "furniture": [
        "Сломан стул в третьем ряду",
        "Шатается парта",
        "Не закрывается дверь, сломан замок",
        "Отвалилась ручка у шкафа",
    ],
    "plumbing": [
        "Течёт кран в туалете",
        "Засор в раковине",
        "Батарея холодная, в кабинете холодно",
        "Протекает радиатор",
    ],
    "other": [
        "Странный запах в кабинете",
        "Нужно повесить стенд",
        "Мусор после ремонта в коридоре",
    ],
}

# Вес категории — как часто такие заявки встречаются.
CATEGORY_WEIGHTS = {"computers": 30, "network": 22, "electric": 18, "furniture": 14, "plumbing": 10, "other": 6}

ASSIGNEES = [
    ("Электрик", ["electric"]),
    ("Сисадмин (сеть)", ["network", "computers"]),
    ("Техник по оборудованию", ["computers"]),
    ("Плотник", ["furniture", "other"]),
    ("Сантехник", ["plumbing", "other"]),
]

ROOMS = ["101", "104", "112", "201", "203", "214", "218", "301", "305", "310", "спортзал", "актовый зал", "библиотека"]


def random_work_time(rng: random.Random, day: datetime) -> datetime:
    """Случайный момент рабочего дня (8:00–17:00 по Москве) в UTC."""
    local = datetime(day.year, day.month, day.day, 8, tzinfo=LOCAL_TZ) + timedelta(minutes=rng.randint(0, 9 * 60))
    return local.astimezone(UTC)


async def seed(db_path: Path, count: int, days: int, seed_value: int) -> None:
    rng = random.Random(seed_value)
    org = load_org_config("config/org.yaml")
    sla = {c.code: c.sla_hours for c in org.categories}

    if db_path.exists():
        db_path.unlink()
    db = await open_db(f"sqlite+aiosqlite:///{db_path}", org)

    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    workdays = [start + timedelta(days=i) for i in range(days + 1) if (start + timedelta(days=i)).weekday() < 5]

    async with db.session() as s, s.begin():
        workers = []
        for i, (name, cats) in enumerate(ASSIGNEES, start=1):
            a = Assignee(org_id=org.org_id, name=name, max_id=-1000 - i, active=True)
            s.add(a)
            workers.append((a, cats))
        await s.flush()

        created_times = sorted(random_work_time(rng, rng.choice(workdays)) for _ in range(count))
        created_times = [t for t in created_times if t < now]

        for n, created in enumerate(created_times, start=1):
            category = rng.choices(list(CATEGORY_WEIGHTS), weights=list(CATEGORY_WEIGHTS.values()))[0]
            applicant_id = -rng.randint(1, 120)
            req = await repo.create_request(
                s,
                org_id=org.org_id,
                building=rng.choice(org.buildings).code,
                room=rng.choice(ROOMS),
                description=rng.choice(DESCRIPTIONS[category]),
                category=category,
                applicant_max_id=applicant_id,
                applicant_name=f"Тестовый заявитель {-applicant_id}",
                now=created,
            )

            # Время реакции: обычно быстро, иногда долго (хвост распределения).
            taken = created + timedelta(minutes=rng.lognormvariate(3.2, 0.9))
            if rng.random() < 0.04 and taken < now:
                await repo.set_status(s, req, Status.CANCELLED, changed_by=applicant_id, now=taken)
                continue
            # Часть свежих заявок ещё ждёт в очереди.
            if taken >= now or (now - created < timedelta(days=2) and rng.random() < 0.35):
                continue

            suitable = [a for a, cats in workers if category in cats]
            # Неравномерная нагрузка: первый подходящий берёт чаще.
            worker = rng.choices(suitable, weights=[3, 1, 1][: len(suitable)])[0]
            await repo.set_status(
                s, req, Status.IN_PROGRESS, changed_by=worker.max_id, assignee_id=worker.id, now=taken
            )

            # Изредка заявку взяли и забыли — она висит в работе и становится просроченной.
            if now - created < timedelta(days=12) and rng.random() < 0.04:
                continue

            # Около 15% заявок выходят за норматив.
            factor = rng.uniform(1.1, 2.5) if rng.random() < 0.15 else rng.uniform(0.1, 0.9)
            closed = taken + timedelta(hours=sla[category] * factor)
            if closed < now:
                await repo.set_status(s, req, Status.DONE, changed_by=worker.max_id, now=closed)

    async with db.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT status, count(*) FROM requests GROUP BY status"))).all()
    await db.close()
    print(f"Демо-БД: {db_path}")
    for status, cnt in rows:
        print(f"  {status}: {cnt}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/demo.db", help="путь к демо-БД (будет перезаписана)")
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1, help="зерно генератора для воспроизводимости")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if db_path.name == "bot.db":
        raise SystemExit("Демо-данные нельзя писать в рабочую БД bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(seed(db_path, args.count, args.days, args.seed))


if __name__ == "__main__":
    main()
