"""FastAPI-приложение дашборда: страница и JSON с метриками."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url

from app import stats
from app.config import load_settings
from app.db import open_db

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.org = settings.org
    app.state.db = await open_db(settings.database_url, settings.org)
    db_name = Path(make_url(settings.database_url).database or "").name
    app.state.demo = "demo" in db_name
    yield
    await app.state.db.close()


app = FastAPI(title="Дашборд диспетчерской", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
async def api_stats(days: int = Query(30, ge=1, le=365)) -> dict:
    async with app.state.db.session() as s:
        data = await stats.collect(s, app.state.org, days)
    data["demo"] = app.state.demo
    return data
