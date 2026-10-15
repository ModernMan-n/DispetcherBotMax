"""Модель данных. Во всех таблицах есть org_id — задел под несколько организаций."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, TypeDecorator
from sqlalchemy import text as sa_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Хранит время в UTC; SQLite теряет таймзону, поэтому возвращаем её обратно."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("Ожидается datetime с таймзоной")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


# Разрешённые переходы статусов.
TRANSITIONS: dict[Status, set[Status]] = {
    Status.NEW: {Status.IN_PROGRESS, Status.CANCELLED},
    Status.IN_PROGRESS: {Status.DONE, Status.CANCELLED, Status.NEW},
    Status.DONE: set(),
    Status.CANCELLED: set(),
}

ACTIVE_STATUSES = (Status.NEW, Status.IN_PROGRESS)


class Role(StrEnum):
    """Роль сотрудника в боте и веб-интерфейсе."""

    ADMIN = "admin"  # всё, включая управление сотрудниками
    DISPATCHER = "dispatcher"  # заявки: назначение, правка, сроки, приоритет; категории
    SPECIALIST = "specialist"  # работает с заявками
    VIEWER = "viewer"  # руководство: только смотрит аналитику и заявки


# Кто работает с заявками (получает их в личку, берёт, закрывает)
WORKING_ROLES = (Role.ADMIN, Role.DISPATCHER, Role.SPECIALIST)


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# Для сортировки: срочные — первыми
PRIORITY_RANK = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.NORMAL: 2, Priority.LOW: 3}


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    sla_hours: Mapped[int]
    # Ключевые слова для автоопределения, JSON-список. NULL — ещё не заполнялись из конфига.
    keywords: Mapped[str | None] = mapped_column(Text)
    # «Прочее»: сюда попадает заявка, если ничего не подошло. Ровно одна на организацию.
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("0"))
    # Отключённую нельзя выбрать для новых заявок, но старые заявки её сохраняют
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("1"))
    sort_order: Mapped[int] = mapped_column(default=0, server_default=sa_text("0"))

    __table_args__ = (Index("ux_categories_org_code", "org_id", "code", unique=True),)


class Assignee(Base):
    """Исполнитель. Специалист — исполнитель с телефоном и направлениями, его заводит администратор.

    Тот, кто просто нажал «Взять» в чате диспетчерской, тоже становится исполнителем, но без телефона.
    """

    __tablename__ = "assignees"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # MAX ID появляется, когда специалист подтвердил номер в боте
    max_id: Mapped[int | None] = mapped_column(BigInteger)
    # Только цифры, формат 7XXXXXXXXXX
    phone: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # «На линии»: получает уведомления о новых заявках своих направлений
    on_duty: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(16), default=Role.SPECIALIST)

    directions: Mapped[list[AssigneeDirection]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ux_assignees_org_phone", "org_id", "phone", unique=True),
        Index("ix_assignees_org_max_id", "org_id", "max_id"),
    )

    @property
    def is_specialist(self) -> bool:
        return self.phone is not None

    @property
    def direction_codes(self) -> list[str]:
        return [d.category for d in self.directions]

    @property
    def works_requests(self) -> bool:
        return self.role in WORKING_ROLES


class AssigneeDirection(Base):
    """Направления специалиста — это категории заявок (config/org.yaml)."""

    __tablename__ = "assignee_directions"

    assignee_id: Mapped[int] = mapped_column(ForeignKey("assignees.id", ondelete="CASCADE"), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), primary_key=True)


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    building: Mapped[str] = mapped_column(String(64))
    room: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    # auto — определено ботом, manual — исправлено диспетчером
    category_source: Mapped[str] = mapped_column(String(16), default="auto")
    status: Mapped[str] = mapped_column(String(16), default=Status.NEW)
    applicant_max_id: Mapped[int] = mapped_column(BigInteger)
    applicant_name: Mapped[str] = mapped_column(String(128))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("assignees.id"))
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    priority: Mapped[str] = mapped_column(String(16), default=Priority.NORMAL, server_default=Priority.NORMAL.value)
    # Срок выполнения: по умолчанию — создание + норматив категории; диспетчер может перенести
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Срок задан вручную — тогда при смене категории не пересчитывается
    due_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("0"))

    # Первый (основной) исполнитель; вся команда — в team. assignee_id всегда = team[0].id
    assignee: Mapped[Assignee | None] = relationship(lazy="selectin")
    team: Mapped[list[Assignee]] = relationship(
        secondary="request_assignees", order_by="RequestAssignee.added_at", lazy="selectin", viewonly=True,
    )
    log: Mapped[list[StatusLog]] = relationship(
        back_populates="request", order_by="StatusLog.changed_at", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_requests_org_status", "org_id", "status"),
        Index("ix_requests_org_applicant", "org_id", "applicant_max_id"),
        Index("ix_requests_org_created", "org_id", "created_at"),
    )

    def work_time(self) -> timedelta | None:
        """Время выполнения: от назначения исполнителя (взял или назначили) до «Готово»."""
        if self.closed_at is None or self.taken_at is None:
            return None
        return self.closed_at - self.taken_at

    def is_overdue(self, now: datetime) -> bool:
        return self.status in ACTIVE_STATUSES and self.due_at is not None and now > self.due_at


class StatusLog(Base):
    __tablename__ = "status_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # MAX ID того, кто сменил статус (заявитель, диспетчер, исполнитель)
    changed_by: Mapped[int | None] = mapped_column(BigInteger)

    request: Mapped[Request] = relationship(back_populates="log")


class CardMessage(Base):
    """Где лежат карточки заявки (чат диспетчерской, личка специалистов) — чтобы обновлять их все."""

    __tablename__ = "card_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(64))
    # Ровно одно из двух: групповой чат или личка специалиста
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LoginCode(Base):
    """Одноразовый код входа в веб-интерфейс, бот присылает его в личку MAX. Храним только хеш."""

    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("assignees.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class WebSession(Base):
    """Сессия веб-интерфейса. В cookie лежит случайный токен, в БД — только его хеш."""

    __tablename__ = "web_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("assignees.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    assignee: Mapped[Assignee] = relationship(lazy="selectin")


class RequestEvent(Base):
    """История изменений заявки, кроме статусов: срок, приоритет, правка полей, категория, назначение."""

    __tablename__ = "request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # due | priority | edit | category | assign
    text: Mapped[str] = mapped_column(Text)  # что изменилось, для людей
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("assignees.id"))
    actor_name: Mapped[str | None] = mapped_column(String(128))


class RequestAssignee(Base):
    """Исполнители заявки: их может быть несколько (назначает диспетчер)."""

    __tablename__ = "request_assignees"

    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), primary_key=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("assignees.id"), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
