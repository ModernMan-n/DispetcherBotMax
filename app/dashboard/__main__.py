"""Дашборд руководства: python -m app.dashboard [--db data/demo.db] [--port 8000]

По умолчанию слушает только localhost. На сервере (в Docker) — 0.0.0.0 за прокси Caddy с HTTPS:
DASHBOARD_HOST=0.0.0.0, TRUST_PROXY=1 — тогда схема https и адрес клиента берутся из заголовков прокси.
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
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    args = parser.parse_args()
    if args.db:
        path = Path(args.db)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise SystemExit(f"Нет файла БД: {path}")
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    behind_proxy = os.getenv("TRUST_PROXY") == "1"
    uvicorn.run(
        "app.dashboard.web:app",
        host=args.host,
        port=args.port,
        proxy_headers=behind_proxy,
        # Внутри docker-сети к дашборду ходит только Caddy
        forwarded_allow_ips="*" if behind_proxy else None,
    )


if __name__ == "__main__":
    main()
