"""Дашборд руководства: python -m app.dashboard [--db data/demo.db] [--port 8000]

Слушает только localhost: авторизации пока нет, наружу открывать нельзя.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from app.config import ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="путь к SQLite-БД (по умолчанию — из DATABASE_URL, то есть рабочая БД бота)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.db:
        path = Path(args.db)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise SystemExit(f"Нет файла БД: {path}")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    uvicorn.run("app.dashboard.web:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
