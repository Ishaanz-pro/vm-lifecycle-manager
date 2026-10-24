"""SQLAlchemy models. The database is the source of truth for control-plane state."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from vmlm.domain.states import VMState


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Stores naive UTC (SQLite has no tz support) and returns tz-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (
        CheckConstraint("vcpus > 0", name="ck_instances_vcpus_positive"),
        CheckConstraint("memory_mib > 0", name="ck_instances_memory_positive"),
        Index("ix_instances_state", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    vcpus: Mapped[int] = mapped_column(Integer)
    memory_mib: Mapped[int] = mapped_column(Integer)
    state: Mapped[VMState] = mapped_column(Enum(VMState, native_enum=False, length=16))
    provider_id: Mapped[str] = mapped_column(String(80))
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic concurrency token
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Reservation(Base):
    """Capacity held by one instance. PK on instance_id makes double allocation impossible;
    release deletes the row, so a second release finds nothing."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("vcpus > 0", name="ck_reservations_vcpus_positive"),
        CheckConstraint("memory_mib > 0", name="ck_reservations_memory_positive"),
    )

    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)
    vcpus: Mapped[int] = mapped_column(Integer)
    memory_mib: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_instance", "instance_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(16), default=None)
    to_state: Mapped[str | None] = mapped_column(String(16), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
