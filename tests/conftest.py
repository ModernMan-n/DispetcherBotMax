from __future__ import annotations

import pytest

from app.config import load_org_config
from app.db import open_db


@pytest.fixture
def org():
    return load_org_config("config/org.yaml")


@pytest.fixture
async def db(org):
    database = await open_db("sqlite+aiosqlite:///:memory:", org)
    yield database
    await database.close()
