"""Резервная копия рабочей БД (SQLite) — безопасно и на работающем боте.

    python -m scripts.backup               # data/backup/bot-ГГГГ-ММ-ДД_ЧЧММ.db, хранит 30 последних
    python -m scripts.backup --keep 60

На сервере запускается по расписанию (cron), см. deploy/README.md.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import load_settings
from app.db import resolve_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", type=int, default=30, help="сколько последних копий хранить")
    args = parser.parse_args()

    url = make_url(resolve_url(load_settings().database_url))
    if url.get_backend_name() != "sqlite":
        raise SystemExit("Скрипт для SQLite. Для PostgreSQL используйте pg_dump.")
    src = Path(url.database)
    if not src.exists():
        raise SystemExit(f"Нет базы: {src}")

    folder = src.parent / "backup"
    folder.mkdir(exist_ok=True)
    dst = folder / f"{src.stem}-{datetime.now():%Y-%m-%d_%H%M}.db"
    # backup API SQLite делает согласованную копию, даже если в базу в этот момент пишут
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
    print(f"Копия: {dst} ({dst.stat().st_size // 1024} КБ)")

    copies = sorted(folder.glob(f"{src.stem}-*.db"))
    for old in copies[: max(0, len(copies) - args.keep)]:
        old.unlink()
        print(f"Удалена старая копия: {old.name}")


if __name__ == "__main__":
    main()
