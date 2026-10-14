# Образ для бота и дашборда (одна сборка, разные команды в docker-compose.yml)
FROM python:3.12.7-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока не менялся uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./

# Не root. UID 1000 — чтобы совпадал с владельцем папки data на сервере.
RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app

VOLUME ["/app/data"]
