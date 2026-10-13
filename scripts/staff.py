"""Управление специалистами (пока нет админки на дашборде).

    python -m scripts.staff list
    python -m scripts.staff add "Иван Петров" +79001234567 network computers
    python -m scripts.staff add "Директор" +79001112233 --role viewer
    python -m scripts.staff role 3 admin     # роли: admin, specialist, viewer
    python -m scripts.staff directions 3 network 1c
    python -m scripts.staff deactivate 3
    python -m scripts.staff activate 3
    python -m scripts.staff unlink 3        # отвязать аккаунт MAX (например, сменил телефон)

Направления — коды категорий из config/org.yaml. Работает с рабочей БД бота (DATABASE_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app import auth, staff
from app.config import load_settings
from app.db import open_db
from app.models import Role


def _check_directions(org, codes: list[str]) -> None:
    unknown = [c for c in codes if c not in org.category_titles]
    if unknown:
        known = ", ".join(f"{c} ({t})" for c, t in org.category_titles.items())
        sys.exit(f"Неизвестные направления: {', '.join(unknown)}\nДоступны: {known}")


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    org = settings.org
    db = await open_db(settings.database_url, org)
    try:
        async with db.session() as s, s.begin():
            if args.cmd == "list":
                rows = await staff.list_specialists(s, org.org_id)
                if not rows:
                    print("Специалистов нет. Добавить: python -m scripts.staff add ИМЯ ТЕЛЕФОН НАПРАВЛЕНИЯ...")
                for a in rows:
                    dirs = ", ".join(org.category_titles.get(c, c) for c in a.direction_codes) or "—"
                    flags = [
                        auth.ROLE_TITLES[Role(a.role)],
                        "активен" if a.active else "ОТКЛЮЧЁН",
                        "вошёл в бот" if a.max_id else "ещё не входил",
                        "на линии" if a.on_duty else "не на линии",
                    ]
                    print(f"{a.id:>3}  {a.name:<24} {staff.mask_phone(a.phone)}  {dirs}  [{', '.join(flags)}]")
                return

            if args.cmd == "add":
                _check_directions(org, args.directions)
                try:
                    a = await staff.add_specialist(
                        s, org.org_id, name=args.name, phone=args.phone, directions=args.directions,
                        role=Role(args.role),
                    )
                except staff.StaffError as e:
                    sys.exit(str(e))
                print(f"Добавлен специалист №{a.id}: {a.name}. Пусть напишет боту /staff и поделится номером.")
                return

            a = await staff.get_specialist_by_id(s, org.org_id, args.id)
            if a is None:
                sys.exit(f"Нет специалиста №{args.id}")
            if args.cmd == "directions":
                _check_directions(org, args.directions)
                await staff.set_directions(s, a, args.directions)
            elif args.cmd == "deactivate":
                a.active, a.on_duty = False, False
                await auth.end_all_sessions(s, a.id)
            elif args.cmd == "activate":
                a.active = True
            elif args.cmd == "unlink":
                a.max_id, a.on_duty = None, False
                await auth.end_all_sessions(s, a.id)
            elif args.cmd == "role":
                a.role = Role(args.role)
                if not a.works_requests:
                    a.on_duty = False
                await auth.end_all_sessions(s, a.id)
            print("Готово.")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="список специалистов")
    p = sub.add_parser("add", help="добавить специалиста")
    p.add_argument("name")
    p.add_argument("phone")
    p.add_argument("directions", nargs="*", help="коды категорий из config/org.yaml")
    p.add_argument("--role", choices=[r.value for r in Role], default=Role.SPECIALIST.value)
    p = sub.add_parser("directions", help="заменить направления")
    p.add_argument("id", type=int)
    p.add_argument("directions", nargs="+")
    p = sub.add_parser("role", help="сменить роль")
    p.add_argument("id", type=int)
    p.add_argument("role", choices=[r.value for r in Role])
    for cmd, help_ in (("deactivate", "отключить"), ("activate", "включить"), ("unlink", "отвязать аккаунт MAX")):
        sub.add_parser(cmd, help=help_).add_argument("id", type=int)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
