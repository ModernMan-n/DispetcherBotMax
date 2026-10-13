"""Веб-интерфейс: вход по коду из MAX, роли, аналитика, заявки, специалисты.

Права проверяются на сервере для каждого запроса — интерфейс лишь прячет недоступные кнопки.
Демо-база (имя файла с «demo») открывается без входа и только для просмотра.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from maxapi import Bot
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app import actions, auth, staff, stats
from app.auth import Perm
from app.config import load_settings
from app.db import open_db
from app.formatting import format_duration
from app.membership import MembershipChecker
from app.models import ACTIVE_STATUSES, Assignee, Request as Req, Role, Status, utcnow

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
COOKIE = "dsess"
CODE_REQUESTS_PER_IP = (10, 600)  # не больше 10 запросов кода за 10 минут с одного адреса


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.org = settings.org
    app.state.db = await open_db(settings.database_url, settings.org)
    db_name = Path(make_url(settings.database_url).database or "").name
    app.state.demo = "demo" in db_name
    # Бот нужен веб-интерфейсу, чтобы присылать коды входа и обновлять карточки в MAX.
    # Сообщения шлём через API, события не читаем — второй копией бота это не является.
    app.state.bot = Bot(token=settings.bot_token) if settings.bot_token else None
    app.state.members = MembershipChecker(settings.org.staff_chat_id)
    app.state.code_requests = defaultdict(deque)
    yield
    await app.state.db.close()


app = FastAPI(title="Дашборд диспетчерской", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def ctx() -> actions.Ctx:
    return actions.Ctx(bot=app.state.bot, db=app.state.db, org=app.state.org, members=app.state.members)


# --- защита от подделки запросов с чужих сайтов ---


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    if request.method in ("POST", "PATCH", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"detail": "Запрос с чужого сайта"}, status_code=403)
        # Чужой сайт может отправить «простой» POST (форма), но не JSON и не PATCH/PUT/DELETE —
        # для них браузер сначала спрашивает разрешение (CORS), а мы его не даём.
        if request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "Ожидается JSON"}, status_code=415)
    return await call_next(request)


# --- кто вошёл ---


def _demo_viewer() -> Assignee:
    return Assignee(id=0, org_id=app.state.org.org_id, name="Демо-просмотр", role=Role.VIEWER, active=True)


async def current_user(request: Request) -> Assignee:
    if app.state.demo:
        return _demo_viewer()
    token = request.cookies.get(COOKIE)
    async with app.state.db.session() as s:
        user = await auth.session_user(s, app.state.org.org_id, token)
    if user is None:
        raise HTTPException(401, "Нужно войти")
    if app.state.bot is not None and not await app.state.members.is_member(app.state.bot, user.max_id):
        raise HTTPException(403, "Доступ только сотрудникам колледжа")
    return user


def require(perm: Perm):
    async def dep(user: Assignee = Depends(current_user)) -> Assignee:
        if perm not in auth.perms_of(user):
            raise HTTPException(403, "Недостаточно прав")
        return user

    return dep


def user_info(a: Assignee) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "role_title": auth.ROLE_TITLES[Role(a.role)],
        "perms": sorted(auth.perms_of(a)),
        "demo": app.state.demo,
        "org_name": app.state.org.name,
    }


# --- страницы ---


@app.get("/")
async def index(request: Request):
    if not app.state.demo:
        async with app.state.db.session() as s:
            if await auth.session_user(s, app.state.org.org_id, request.cookies.get(COOKIE)) is None:
                return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page():
    if app.state.demo:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


# --- вход ---


class CodeRequest(BaseModel):
    phone: str = Field(max_length=32)


class CodeVerify(BaseModel):
    phone: str = Field(max_length=32)
    code: str = Field(max_length=12)


GENERIC_SENT = (
    "Если номер есть в списке сотрудников и вы уже входили в бота через /staff, "
    "код придёт в личные сообщения MAX."
)


def _rate_limited(ip: str) -> bool:
    limit, window = CODE_REQUESTS_PER_IP
    q = app.state.code_requests[ip]
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


@app.post("/api/auth/request")
async def auth_request(body: CodeRequest, request: Request) -> dict:
    if app.state.bot is None:
        raise HTTPException(503, "Бот не настроен: код отправить нельзя")
    if staff.normalize_phone(body.phone) is None:
        raise HTTPException(422, "Введите номер телефона, например +7 900 123-45-67")
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")
    async with app.state.db.session() as s, s.begin():
        try:
            found = await auth.request_code(s, app.state.org.org_id, body.phone)
        except auth.AuthError as e:
            raise HTTPException(429, str(e)) from e
    if found is not None:
        a, code = found
        if await app.state.members.is_member(app.state.bot, a.max_id):
            try:
                await app.state.bot.send_message(
                    user_id=a.max_id,
                    text=f"Код для входа в дашборд диспетчерской: {code}\n"
                         f"Действует 5 минут. Никому его не сообщайте.",
                )
                log.info("Код входа отправлен сотруднику %s", a.id)
            except Exception:
                log.exception("Не удалось отправить код входа сотруднику %s", a.id)
    # Одинаковый ответ, есть номер в списке или нет — чтобы нельзя было перебором узнать сотрудников.
    return {"message": GENERIC_SENT}


@app.post("/api/auth/verify")
async def auth_verify(body: CodeVerify, response: Response, request: Request) -> dict:
    async with app.state.db.session() as s:
        try:
            a = await auth.verify_code(s, app.state.org.org_id, body.phone, body.code)
        except auth.AuthError as e:
            await s.commit()  # счётчик попыток сохраняем и при ошибке
            raise HTTPException(401, str(e)) from e
        if app.state.bot is not None and not await app.state.members.is_member(app.state.bot, a.max_id):
            await s.commit()
            raise HTTPException(403, "Доступ только сотрудникам колледжа")
        token = await auth.create_session(s, a.id)
        await s.commit()
    response.set_cookie(
        COOKIE, token, max_age=int(auth.SESSION_TTL.total_seconds()), httponly=True,
        samesite="strict", secure=request.url.scheme == "https", path="/",
    )
    log.info("Вход в веб-интерфейс: сотрудник %s (%s)", a.id, a.role)
    return user_info(a)


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict:
    async with app.state.db.session() as s, s.begin():
        await auth.end_session(s, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(user: Assignee = Depends(current_user)) -> dict:
    return user_info(user)


# --- аналитика ---


@app.get("/api/stats")
async def api_stats(days: int = Query(30, ge=1, le=365), user: Assignee = Depends(require(Perm.VIEW))) -> dict:
    async with app.state.db.session() as s:
        data = await stats.collect(s, app.state.org, days)
    data["demo"] = app.state.demo
    return data


# --- заявки ---


@app.get("/api/meta")
async def meta(user: Assignee = Depends(require(Perm.VIEW))) -> dict:
    org = app.state.org
    assignees = []
    if Perm.ADMIN_REQUESTS in auth.perms_of(user):
        async with app.state.db.session() as s:
            assignees = [
                {"id": a.id, "name": a.name, "directions": a.direction_codes}
                for a in await staff.list_specialists(s, org.org_id)
                if a.active and a.works_requests
            ]
    return {
        "buildings": [{"code": b.code, "title": b.title} for b in org.buildings],
        "categories": [{"code": c.code, "title": c.title} for c in org.categories],
        "roles": [{"code": r.value, "title": auth.ROLE_TITLES[r]} for r in Role],
        "assignees": assignees,
    }


def request_row(r: Req, user: Assignee, now: datetime) -> dict:
    org = app.state.org
    tz = ZoneInfo(org.timezone)
    sla = next((c.sla_hours for c in org.categories if c.code == r.category), None)
    age = now - r.created_at
    row = {
        "id": r.id,
        "created_at": r.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        "category": r.category,
        "category_title": org.category_titles.get(r.category, r.category),
        "category_source": r.category_source,
        "building": r.building,
        "building_title": org.building_titles.get(r.building, r.building),
        "room": r.room,
        "description": r.description,
        "status": r.status,
        "assignee_id": r.assignee_id,
        "assignee": r.assignee.name if r.assignee else None,
        "age": format_duration(age if r.closed_at is None else r.closed_at - r.created_at),
        "overdue": r.status in ACTIVE_STATUSES and sla is not None and age.total_seconds() > sla * 3600,
    }
    # Имя заявителя видят только те, кто работает с заявками
    if Perm.WORK in auth.perms_of(user):
        row["applicant"] = r.applicant_name
    return row


@app.get("/api/requests")
async def list_requests(
    scope: str = Query("active", pattern="^(active|mine|closed)$"),
    limit: int = Query(200, ge=1, le=500),
    user: Assignee = Depends(require(Perm.VIEW)),
) -> dict:
    q = select(Req).where(Req.org_id == app.state.org.org_id)
    if scope == "active":
        q = q.where(Req.status.in_(ACTIVE_STATUSES)).order_by(Req.created_at)
    elif scope == "mine":
        q = q.where(Req.status.in_(ACTIVE_STATUSES), Req.assignee_id == user.id).order_by(Req.created_at)
    else:
        q = q.where(Req.status.not_in(ACTIVE_STATUSES)).order_by(Req.closed_at.desc())
    async with app.state.db.session() as s:
        rows = list(await s.scalars(q.limit(limit)))
        total = await s.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    now = utcnow()
    return {"items": [request_row(r, user, now) for r in rows], "total": total}


class ActionBody(BaseModel):
    action: str = Field(pattern="^(take|done|release|cancel)$")


class CategoryBody(BaseModel):
    category: str = Field(max_length=64)


class AssignBody(BaseModel):
    assignee_id: int


class EditBody(BaseModel):
    building: str | None = Field(None, max_length=64)
    room: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=2000)


async def _run(coro, user: Assignee) -> dict:
    try:
        req = await coro
    except actions.ActionError as e:
        raise HTTPException(409, str(e)) from e
    return request_row(req, user, utcnow())


@app.post("/api/requests/{request_id}/action")
async def request_action(request_id: int, body: ActionBody, user: Assignee = Depends(require(Perm.WORK))) -> dict:
    return await _run(actions.change_status(ctx(), request_id, body.action, user), user)


@app.post("/api/requests/{request_id}/category")
async def request_category(request_id: int, body: CategoryBody, user: Assignee = Depends(require(Perm.WORK))) -> dict:
    return await _run(actions.set_category(ctx(), request_id, body.category, user), user)


@app.post("/api/requests/{request_id}/assign")
async def request_assign(
    request_id: int, body: AssignBody, user: Assignee = Depends(require(Perm.ADMIN_REQUESTS))
) -> dict:
    return await _run(actions.assign(ctx(), request_id, body.assignee_id, user), user)


@app.patch("/api/requests/{request_id}")
async def request_edit(
    request_id: int, body: EditBody, user: Assignee = Depends(require(Perm.ADMIN_REQUESTS))
) -> dict:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "Нечего менять")
    return await _run(actions.edit_request(ctx(), request_id, fields, user), user)


# --- специалисты (только администратор) ---


def staff_row(a: Assignee) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "phone": a.phone,
        "role": a.role,
        "role_title": auth.ROLE_TITLES[Role(a.role)],
        "directions": a.direction_codes,
        "active": a.active,
        "linked": a.max_id is not None,
        "on_duty": a.on_duty,
    }


class StaffBody(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    phone: str = Field(max_length=32)
    role: Role = Role.SPECIALIST
    directions: list[str] = []


class StaffPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=128)
    phone: str | None = Field(None, max_length=32)
    role: Role | None = None
    directions: list[str] | None = None
    active: bool | None = None


def _check_directions(codes: list[str]) -> None:
    unknown = [c for c in codes if c not in app.state.org.category_titles]
    if unknown:
        raise HTTPException(422, f"Неизвестные направления: {', '.join(unknown)}")


@app.get("/api/staff")
async def staff_list(user: Assignee = Depends(require(Perm.MANAGE_STAFF))) -> dict:
    async with app.state.db.session() as s:
        rows = await staff.list_specialists(s, app.state.org.org_id)
    return {"items": [staff_row(a) for a in rows]}


@app.post("/api/staff")
async def staff_create(body: StaffBody, user: Assignee = Depends(require(Perm.MANAGE_STAFF))) -> dict:
    _check_directions(body.directions)
    async with app.state.db.session() as s, s.begin():
        try:
            a = await staff.add_specialist(
                s, app.state.org.org_id, name=body.name.strip(), phone=body.phone,
                directions=body.directions, role=body.role,
            )
        except staff.StaffError as e:
            raise HTTPException(409, str(e)) from e
    log.info("Веб: сотрудник %s добавлен администратором %s", a.id, user.id)
    return staff_row(a)


@app.patch("/api/staff/{staff_id}")
async def staff_update(staff_id: int, body: StaffPatch, user: Assignee = Depends(require(Perm.MANAGE_STAFF))) -> dict:
    changes = body.model_dump(exclude_none=True)
    if staff_id == user.id and (changes.get("active") is False or changes.get("role", Role.ADMIN) != Role.ADMIN):
        raise HTTPException(409, "Нельзя отключить себя или снять с себя роль администратора")
    async with app.state.db.session() as s, s.begin():
        a = await staff.get_specialist_by_id(s, app.state.org.org_id, staff_id)
        if a is None:
            raise HTTPException(404, "Сотрудник не найден")
        logout = False
        if "name" in changes:
            a.name = changes["name"].strip()
        if "phone" in changes:
            norm = staff.normalize_phone(changes["phone"])
            if norm is None:
                raise HTTPException(422, "Не похоже на номер телефона")
            if norm != a.phone:
                taken = await s.scalar(
                    select(Assignee).where(Assignee.org_id == a.org_id, Assignee.phone == norm, Assignee.id != a.id)
                )
                if taken is not None:
                    raise HTTPException(409, "Этот номер уже у другого сотрудника")
                # Новый номер нужно заново подтвердить в боте
                a.phone, a.max_id, a.on_duty = norm, None, False
                logout = True
        if "role" in changes and changes["role"] != a.role:
            a.role = changes["role"]
            if not a.works_requests:
                a.on_duty = False
            logout = True
        if "directions" in changes:
            _check_directions(changes["directions"])
            await staff.set_directions(s, a, changes["directions"])
        if "active" in changes:
            a.active = changes["active"]
            if not a.active:
                a.on_duty = False
                logout = True
        if logout:
            await auth.end_all_sessions(s, a.id)
    log.info("Веб: сотрудник %s изменён администратором %s: %s", a.id, user.id, sorted(changes))
    return staff_row(a)


@app.delete("/api/staff/{staff_id}")
async def staff_delete(staff_id: int, user: Assignee = Depends(require(Perm.MANAGE_STAFF))) -> dict:
    if staff_id == user.id:
        raise HTTPException(409, "Нельзя удалить себя")
    async with app.state.db.session() as s, s.begin():
        a = await staff.get_specialist_by_id(s, app.state.org.org_id, staff_id)
        if a is None:
            raise HTTPException(404, "Сотрудник не найден")
        has_history = await s.scalar(select(func.count()).select_from(Req).where(Req.assignee_id == a.id))
        await auth.end_all_sessions(s, a.id)
        if has_history:
            # По его заявкам считается аналитика — запись оставляем, но отключаем.
            a.active, a.on_duty, a.max_id = False, False, None
            result = {"deleted": False, "archived": True}
        else:
            await s.delete(a)
            result = {"deleted": True, "archived": False}
    log.info("Веб: сотрудник %s удалён администратором %s (%s)", staff_id, user.id, result)
    return result
