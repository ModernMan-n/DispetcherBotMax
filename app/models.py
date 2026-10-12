"""Модель данных. Во всех таблицах есть org_id — задел под несколько организаций."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, TypeDecorator
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


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    sla_hours: Mapped[int]

    __table_args__ = (Index("ux_categories_org_code", "org_id", "code", unique=True),)


class Assignee(Base):
    __tablename__ = "assignees"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    max_id: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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

    assignee: Mapped[Assignee | None] = relationship(lazy="selectin")
    log: Mapped[list[StatusLog]] = relationship(
        back_populates="request", order_by="StatusLog.changed_at", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_requests_org_status", "org_id", "status"),
        Index("ix_requests_org_applicant", "org_id", "applicant_max_id"),
        Index("ix_requests_org_created", "org_id", "created_at"),
    )


class StatusLog(Base):
    __tablename__ = "status_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # MAX ID того, кто сменил статус (заявитель, диспетчер, исполнитель)
    changed_by: Mapped[int | None] = mapped_column(BigInteger)

    request: Mapped[Request] = relationship(back_populates="log")
