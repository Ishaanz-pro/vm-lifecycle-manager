"""Shared primitives used by the service, worker and reconciler: transactions, lookups,
state transitions and audit records. Keeping them here guarantees every state change goes
through the same checks and leaves the same audit trail."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import AuditEvent, Instance, Operation, utcnow
from vmlm.db.session import READ_ONLY_OPTION
from vmlm.domain.errors import ConcurrentModification, InstanceNotFound, OperationNotFound
from vmlm.domain.states import VMState, assert_transition
from vmlm.observability.context import request_id_var

log = logging.getLogger("vmlm.core")

Clock = Callable[[], datetime]


@contextmanager
def tx(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Write unit of work: BEGIN IMMEDIATE on SQLite, committed on success."""
    with session_factory() as session, session.begin():
        yield session


@contextmanager
def read_tx(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Read-only unit of work: deferred BEGIN, consistent snapshot, always rolled back.

    Flushing a change inside it is a bug; the session is created with autoflush off and any
    pending change is discarded, so a misuse cannot silently commit.
    """
    with session_factory(autoflush=False) as session:
        session.connection(execution_options={READ_ONLY_OPTION: True})
        try:
            yield session
        finally:
            # Detach first: rollback() expires attached objects, and callers use the loaded
            # values after the unit of work ends. Detached objects keep them; changes are lost.
            session.expunge_all()
            session.rollback()


def get_instance(s: Session, instance_id: str) -> Instance:
    inst = s.get(Instance, instance_id)
    if inst is None:
        raise InstanceNotFound(f"instance {instance_id} not found", {"instance_id": instance_id})
    return inst


def get_operation(s: Session, operation_id: str) -> Operation:
    op = s.get(Operation, operation_id)
    if op is None:
        raise OperationNotFound(
            f"operation {operation_id} not found", {"operation_id": operation_id}
        )
    return op


def audit(
    s: Session,
    instance_id: str,
    event_type: str,
    *,
    from_state: VMState | str | None = None,
    to_state: VMState | str | None = None,
    detail: str | None = None,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> None:
    s.add(
        AuditEvent(
            instance_id=instance_id,
            operation_id=operation_id,
            event_type=event_type,
            from_state=str(from_state) if from_state else None,
            to_state=str(to_state) if to_state else None,
            detail=detail,
            request_id=request_id_var.get(),
            created_at=now or utcnow(),
        )
    )


def apply_transition(
    s: Session,
    inst: Instance,
    target: VMState,
    *,
    action: str | None = None,
    expected_version: int | None = None,
    detail: str | None = None,
    failure_reason: str | None = None,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Validate against the transition table, bump the version, write the audit record."""
    if expected_version is not None and inst.version != expected_version:
        raise ConcurrentModification(
            f"instance {inst.id} was modified concurrently",
            {"instance_id": inst.id, "expected_version": expected_version, "version": inst.version},
        )
    assert_transition(inst.state, target, action)
    previous = inst.state
    inst.state = target
    inst.version += 1
    inst.updated_at = now or utcnow()
    if failure_reason is not None:
        inst.failure_reason = failure_reason
    audit(
        s,
        inst.id,
        "state_changed",
        from_state=previous,
        to_state=target,
        detail=detail or failure_reason,
        operation_id=operation_id,
        now=now,
    )
    log.info(
        "state_changed",
        extra={"instance_id": inst.id, "from_state": previous, "to_state": target},
    )
