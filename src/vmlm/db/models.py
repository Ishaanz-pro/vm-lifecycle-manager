"""SQLAlchemy models. The database is the source of truth for control-plane state.

Tables
  hosts             schedulable capacity + health
  instances         observed state, desired state, placement, optimistic version
  reservations      capacity held per instance (PK = instance_id => no double allocation)
  operations        durable lifecycle work items (claimed by workers under a lease)
  audit_events      append-only history
  idempotency_keys  (key, scope) -> operation, with expiry
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from vmlm.domain.states import DesiredState, OperationAction, OperationStatus, VMState


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


def _enum(kind: type[StrEnum], length: int = 16) -> Enum:
    return Enum(
        kind, native_enum=False, length=length, values_callable=lambda e: [m.value for m in e]
    )


class Base(DeclarativeBase):
    pass


class HostStatus(StrEnum):
    READY = "READY"
    DRAINING = "DRAINING"  # keeps existing instances, accepts no new placements
    UNHEALTHY = "UNHEALTHY"  # agent heartbeat missing/failed; accepts no new placements


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = (
        CheckConstraint("total_vcpus > 0", name="ck_hosts_vcpus_positive"),
        CheckConstraint("total_memory_mib > 0", name="ck_hosts_memory_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    total_vcpus: Mapped[int] = mapped_column(Integer)
    total_memory_mib: Mapped[int] = mapped_column(Integer)
    status: Mapped[HostStatus] = mapped_column(_enum(HostStatus), default=HostStatus.READY)
    agent_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


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
    state: Mapped[VMState] = mapped_column(_enum(VMState))
    desired_state: Mapped[DesiredState] = mapped_column(_enum(DesiredState))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"), default=None)
    provider_id: Mapped[str] = mapped_column(String(80))
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic concurrency token
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Reservation(Base):
    """Capacity held by one instance on one host. PK on instance_id makes double allocation
    impossible; release deletes the row, so a second release finds nothing."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("vcpus > 0", name="ck_reservations_vcpus_positive"),
        CheckConstraint("memory_mib > 0", name="ck_reservations_memory_positive"),
        Index("ix_reservations_host", "host_id"),
    )

    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"), primary_key=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    vcpus: Mapped[int] = mapped_column(Integer)
    memory_mib: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Operation(Base):
    """A durable unit of lifecycle work. Written in the same transaction as the request's
    state change, before any provider call, so interrupted work is always discoverable."""

    __tablename__ = "operations"
    __table_args__ = (
        Index("ix_operations_claim", "status", "next_attempt_at"),
        Index("ix_operations_instance", "instance_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    action: Mapped[OperationAction] = mapped_column(_enum(OperationAction))
    status: Mapped[OperationStatus] = mapped_column(_enum(OperationStatus))
    from_state: Mapped[VMState] = mapped_column(_enum(VMState))
    target_state: Mapped[VMState] = mapped_column(_enum(VMState))
    instance_version: Mapped[int] = mapped_column(Integer)  # version the outcome must match
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    error_kind: Mapped[str | None] = mapped_column(String(16), default=None)  # transient/permanent
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    claimed_by: Mapped[str | None] = mapped_column(String(64), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_instance", "instance_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    operation_id: Mapped[str | None] = mapped_column(String(36), default=None)
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(16), default=None)
    to_state: Mapped[str | None] = mapped_column(String(16), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class IdempotencyKey(Base):
    """scope = "create" or "<action>:<instance_id>", so a key cannot collide across actions."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("instances.id"))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
