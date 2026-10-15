"""Веб-интерфейс: вход по коду из MAX, роли, аналитика, заявки, специалисты.

Права проверяются на сервере для каждого запроса — интерфейс лишь прячет недоступные кнопки.
Демо-база (имя файла с «demo») открывается без входа и только для просмотра.
"""

from __future__ import annotations

import json
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
from sqlalchemy import func, or_, select, true
from sqlalchemy.engine import make_url

from app import actions, auth, categories, repo, staff, stats
from app.auth import Perm
from app.config import load_settings
from app.db import open_db
from app.formatting import fmt_local, format_duration
from app.membership import MembershipChecker
from app.models import ACTIVE_STATUSES, Assignee, Priority, RequestAssignee, Role, Status, utcnow
from app.models import Request as Req

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
    app.state.staff_link = None  # узнаём у MAX при первом обращении (staff_invite_link)
    app.state.code_requests = defaultdict(deque)
    yield
    await app.state.db.close()


async def staff_invite_link() -> str | None:
    """Ссылка «открыть бота и войти как сотрудник»: https://max.ru/<бот>?start=staff. Запоминается."""
    if app.state.staff_link is None:
        app.state.staff_link = await _ask_bot_link(app.state.bot) or ""
    return app.state.staff_link or None


async def _ask_bot_link(bot: Bot | None) -> str | None:
    if bot is None:
        return None
    try:
        me = await bot.get_me()
    except Exception:
        log.warning("Не удалось узнать имя бота — ссылки-приглашения не будет", exc_info=True)
        return None
    return f"https://max.ru/{me.username}?start=staff" if me and me.username else None


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
    await categories.refresh(app.state.db, app.state.org)
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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


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
    "Если номер есть в списке сотрудников и вы уже подтвердили его в боте, "
    "код придёт в личные сообщения MAX в течение минуты. "
    "Нет кода — откройте бота по кнопке ниже и поделитесь номером, затем запросите код снова."
)


@app.get("/api/public")
async def public_info() -> dict:
    """Без входа: ссылка на бота для первого входа (на странице входа)."""
    return {"staff_link": await staff_invite_link()}


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
async def api_stats(days: int = Query(30, ge=1, le=365), user: Assignee = Depends(require(Perm.ANALYTICS))) -> dict:
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
        "staff_link": await staff_invite_link(),
        "buildings": [{"code": b.code, "title": b.title} for b in org.buildings],
        "categories": [{"code": c.code, "title": c.title, "active": c.active} for c in org.categories],
        "priorities": [{"code": p.value, "title": actions.PRIORITY_TITLES[p]} for p in
                       (Priority.URGENT, Priority.HIGH, Priority.NORMAL, Priority.LOW)],
        "roles": [{"code": r.value, "title": auth.ROLE_TITLES[r]} for r in Role],
        "assignees": assignees,
    }


def visible_to(user: Assignee):
    """Условие видимости заявки. Специалист: открытые — все; закрытые — своих направлений или взятые им."""
    if Perm.VIEW_ALL in auth.perms_of(user):
        return true()
    return or_(Req.status.in_(ACTIVE_STATUSES), Req.category.in_(user.direction_codes), in_team(user))


def in_team(user: Assignee):
    """Сотрудник среди исполнителей заявки."""
    return Req.id.in_(select(RequestAssignee.request_id).where(RequestAssignee.assignee_id == user.id))


async def visible_request(s, request_id: int, user: Assignee) -> Req:
    req = await s.scalar(select(Req).where(Req.org_id == app.state.org.org_id, Req.id == request_id, visible_to(user)))
    if req is None:
        raise HTTPException(404, "Заявка не найдена")
    return req


def request_row(r: Req, user: Assignee, now: datetime) -> dict:
    org = app.state.org
    tz = ZoneInfo(org.timezone)
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
        "assignee": ", ".join(a.name for a in r.team) or None,
        "assignees": [{"id": a.id, "name": a.name} for a in r.team],
        # Активная — сколько висит; закрытая — время выполнения (от назначения до «Готово»)
        "age": format_duration(age) if r.closed_at is None
        else (format_duration(r.work_time()) if r.work_time() is not None else "—"),
        "overdue": r.is_overdue(now),
        "priority": r.priority,
        "priority_title": actions.PRIORITY_TITLES[Priority(r.priority)],
        "due_at": fmt_local(r.due_at, org.timezone) if r.due_at else None,
        "due_iso": r.due_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if r.due_at else None,
        "due_manual": r.due_manual,
        "due_left": (format_duration(r.due_at - now) if r.due_at and r.due_at > now else None),
    }
    # Имя заявителя видят только те, кто работает с заявками
    if Perm.WORK in auth.perms_of(user):
        row["applicant"] = r.applicant_name
        row["applicant_url"] = f"max://user/{r.applicant_max_id}"  # открывает профиль в приложении MAX
    return row


@app.get("/api/requests")
async def list_requests(
    scope: str = Query("active", pattern="^(active|mine|direction|closed)$"),
    limit: int = Query(200, ge=1, le=500),
    user: Assignee = Depends(require(Perm.VIEW)),
) -> dict:
    q = select(Req).where(Req.org_id == app.state.org.org_id)
    if scope == "active":
        q = q.where(Req.status.in_(ACTIVE_STATUSES)).order_by(*staff.priority_order())
    elif scope == "mine":
        q = q.where(Req.status.in_(ACTIVE_STATUSES), in_team(user)).order_by(*staff.priority_order())
    elif scope == "direction":
        q = q.where(Req.status.in_(ACTIVE_STATUSES), Req.category.in_(user.direction_codes)) \
            .order_by(*staff.priority_order())
    else:
        q = q.where(Req.status.not_in(ACTIVE_STATUSES), visible_to(user)).order_by(Req.closed_at.desc())
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
    # Несколько исполнителей; assignee_id — для совместимости со старым интерфейсом
    assignee_ids: list[int] = Field(default_factory=list, max_length=20)
    assignee_id: int | None = None


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
    ids = body.assignee_ids or ([body.assignee_id] if body.assignee_id else [])
    return await _run(actions.assign(ctx(), request_id, ids, user), user)


@app.patch("/api/requests/{request_id}")
async def request_edit(
    request_id: int, body: EditBody, user: Assignee = Depends(require(Perm.ADMIN_REQUESTS))
) -> dict:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "Нечего менять")
    return await _run(actions.edit_request(ctx(), request_id, fields, user), user)


class PriorityBody(BaseModel):
    priority: Priority


class DueBody(BaseModel):
    # Одно из трёх: новая дата (местное время организации), сдвиг в часах или возврат к нормативу
    due_at: datetime | None = None
    shift_hours: int | None = Field(None, ge=1, le=24 * 90)
    reset: bool = False
    comment: str = Field("", max_length=300)


@app.post("/api/requests/{request_id}/priority")
async def request_priority(
    request_id: int, body: PriorityBody, user: Assignee = Depends(require(Perm.ADMIN_REQUESTS))
) -> dict:
    return await _run(actions.set_priority(ctx(), request_id, body.priority, user), user)


@app.post("/api/requests/{request_id}/due")
async def request_due(request_id: int, body: DueBody, user: Assignee = Depends(require(Perm.ADMIN_REQUESTS))) -> dict:
    due_at = body.due_at
    if due_at is not None and due_at.tzinfo is None:
        # Из формы приходит местное время без часового пояса
        due_at = due_at.replace(tzinfo=ZoneInfo(app.state.org.timezone))
    return await _run(actions.set_due(
        ctx(), request_id, user, due_at=due_at, shift_hours=body.shift_hours, reset=body.reset, comment=body.comment,
    ), user)


@app.get("/api/requests/{request_id}/history")
async def request_history(request_id: int, user: Assignee = Depends(require(Perm.VIEW))) -> dict:
    """История заявки: смены статуса и изменения (срок, приоритет, правка, категория, назначение)."""
    org = app.state.org
    async with app.state.db.session() as s:
        req = await visible_request(s, request_id, user)
        events = await repo.request_events(s, request_id)
        names = {a.max_id: a.name for a in await staff.list_specialists(s, org.org_id) if a.max_id}
    status_titles = {"new": "Создана", "in_progress": "Взята в работу", "done": "Выполнена", "cancelled": "Отменена"}
    items = [
        {"at": e.changed_at, "text": status_titles.get(e.status, e.status),
         # Кто менял статус: сотрудник по имени; заявителя руководству не называем
         "who": names.get(e.changed_by) or (req.applicant_name if e.changed_by == req.applicant_max_id
                                            and Perm.WORK in auth.perms_of(user) else None)}
        for e in req.log
    ] + [{"at": e.created_at, "text": e.text, "who": e.actor_name} for e in events]
    items.sort(key=lambda x: x["at"])
    return {"items": [{**i, "at": fmt_local(i["at"], org.timezone)} for i in items]}


@app.post("/api/requests/{request_id}/contact")
async def request_contact(request_id: int, user: Assignee = Depends(require(Perm.WORK))) -> dict:
    """Прислать мне в MAX ссылку на профиль заявителя (в браузере max:// обычно не открывается)."""
    async with app.state.db.session() as s:
        await visible_request(s, request_id, user)
    try:
        await actions.contact_applicant(ctx(), request_id, user)
    except actions.ActionError as e:
        raise HTTPException(409, str(e)) from e
    return {"message": "Ссылка на профиль заявителя отправлена вам в MAX"}


# --- категории (диспетчер и администратор) ---


def category_row(c) -> dict:
    return {
        "code": c.code, "title": c.name, "sla_hours": c.sla_hours, "active": c.active,
        "fallback": c.fallback, "keywords": json.loads(c.keywords or "[]"),
    }


class CategoryBody(BaseModel):
    title: str = Field(min_length=2, max_length=128)
    sla_hours: int = Field(ge=1, le=24 * 90)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class CategoryPatch(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=128)
    sla_hours: int | None = Field(None, ge=1, le=24 * 90)
    keywords: list[str] | None = Field(None, max_length=100)
    active: bool | None = None


async def _categories_changed() -> None:
    await categories.refresh(app.state.db, app.state.org, force=True)


@app.get("/api/categories")
async def categories_list(user: Assignee = Depends(require(Perm.MANAGE_CATEGORIES))) -> dict:
    async with app.state.db.session() as s:
        rows = await categories.load(s, app.state.org.org_id)
        used = dict((await s.execute(
            select(Req.category, func.count()).where(Req.org_id == app.state.org.org_id).group_by(Req.category)
        )).all())
    return {"items": [{**category_row(c), "requests": used.get(c.code, 0)} for c in rows]}


async def _cat_run(coro_factory, user: Assignee, what: str):
    async with app.state.db.session() as s, s.begin():
        try:
            result = await coro_factory(s)
        except categories.CategoryError as e:
            raise HTTPException(409, str(e)) from e
    await _categories_changed()
    log.info("Веб: категория %s (сотрудник %s)", what, user.id)
    return result


@app.post("/api/categories")
async def categories_create(body: CategoryBody, user: Assignee = Depends(require(Perm.MANAGE_CATEGORIES))) -> dict:
    row = await _cat_run(lambda s: categories.create(
        s, app.state.org.org_id, title=body.title.strip(), sla_hours=body.sla_hours, keywords=body.keywords,
    ), user, "создана")
    return category_row(row)


@app.patch("/api/categories/{code}")
async def categories_update(
    code: str, body: CategoryPatch, user: Assignee = Depends(require(Perm.MANAGE_CATEGORIES))
) -> dict:
    row = await _cat_run(lambda s: categories.update(
        s, app.state.org.org_id, code, body.model_dump(exclude_none=True),
    ), user, f"{code} изменена")
    return category_row(row)


@app.delete("/api/categories/{code}")
async def categories_delete(code: str, user: Assignee = Depends(require(Perm.MANAGE_CATEGORIES))) -> dict:
    return await _cat_run(lambda s: categories.delete(s, app.state.org.org_id, code), user, f"{code} удалена")


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
        has_history = await s.scalar(
            select(func.count()).select_from(RequestAssignee).where(RequestAssignee.assignee_id == a.id)
        ) or await s.scalar(select(func.count()).select_from(Req).where(Req.assignee_id == a.id))
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
