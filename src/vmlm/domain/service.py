"""Request-path service: validates a request, commits the transitional state together with a
durable Operation row, and returns. It never calls the provider; the Worker does that.

Request shape (create / start / stop / terminate), one short transaction:
  * idempotency lookup (key, scope)
  * validate the transition, commit the transitional state (+ reservation for create)
  * insert the Operation (PENDING) and the audit records
Because the operation row is written in the same transaction as the state change, a request
is either fully recorded or not at all; nothing is ever "half accepted".
"""

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from vmlm.config import HostSpec
from vmlm.db.models import (
    AuditEvent,
    Host,
    HostStatus,
    IdempotencyKey,
    Instance,
    Operation,
    utcnow,
)
from vmlm.domain.core import (
    Clock,
    apply_transition,
    audit,
    get_instance,
    get_operation,
    read_tx,
    tx,
)
from vmlm.domain.errors import HostNotFound, IdempotencyConflict
from vmlm.domain.scheduler import ClusterCapacity, Scheduler
from vmlm.domain.states import (
    ACTION_DESIRED,
    ACTION_STATES,
    TERMINAL_OPERATION_STATUSES,
    OperationAction,
    OperationStatus,
    VMState,
)
from vmlm.observability.context import request_id_var

log = logging.getLogger("vmlm.service")


class LifecycleService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        scheduler: Scheduler,
        *,
        max_attempts: int = 3,
        idempotency_ttl_s: int = 86_400,
        clock: Clock = utcnow,
    ) -> None:
        self._sf = session_factory
        self._scheduler = scheduler
        self._max_attempts = max_attempts
        self._idempotency_ttl = timedelta(seconds=idempotency_ttl_s)
        self._clock = clock

    # ---- hosts -----------------------------------------------------------------------------

    def seed_hosts(self, specs: list[HostSpec]) -> None:
        """Upsert configured hosts by name. Capacity changes apply; status is left alone."""
        with tx(self._sf) as s:
            for spec in specs:
                host = s.scalar(select(Host).where(Host.name == spec.name))
                if host is None:
                    s.add(
                        Host(
                            id=str(uuid.uuid4()),
                            name=spec.name,
                            total_vcpus=spec.vcpus,
                            total_memory_mib=spec.memory_mib,
                            agent_managed=spec.agent_managed,
                            status=HostStatus.READY,
                        )
                    )
                else:
                    host.total_vcpus = spec.vcpus
                    host.total_memory_mib = spec.memory_mib
                    host.agent_managed = spec.agent_managed
                    host.updated_at = self._clock()

    def list_hosts(self) -> ClusterCapacity:
        with read_tx(self._sf) as s:
            return self._scheduler.snapshot(s)

    def set_host_status(self, name: str, status: HostStatus) -> Host:
        with tx(self._sf) as s:
            host = self._host_by_name(s, name)
            host.status = status
            host.updated_at = self._clock()
            log.info("host_status_set", extra={"host": name, "status": status})
            return host

    def record_heartbeat(
        self, name: str, *, healthy: bool, vcpus: int | None, memory_mib: int | None
    ) -> Host:
        """Called by the Go host agent. Capacity reported by the agent replaces the static one."""
        with tx(self._sf) as s:
            host = self._host_by_name(s, name)
            now = self._clock()
            host.last_heartbeat_at = now
            host.updated_at = now
            if vcpus:
                host.total_vcpus = vcpus
            if memory_mib:
                host.total_memory_mib = memory_mib
            if host.status is not HostStatus.DRAINING:  # operator intent wins over agent health
                host.status = HostStatus.READY if healthy else HostStatus.UNHEALTHY
            return host

    @staticmethod
    def _host_by_name(s: Session, name: str) -> Host:
        host = s.scalar(select(Host).where(Host.name == name))
        if host is None:
            raise HostNotFound(f"host {name} not found", {"host": name})
        return host

    # ---- requests --------------------------------------------------------------------------

    def create_instance(
        self, name: str, vcpus: int, memory_mib: int, idempotency_key: str | None = None
    ) -> tuple[Operation, bool]:
        """Returns (operation, created). created=False means an idempotent replay."""
        request_hash = _hash({"name": name, "vcpus": vcpus, "memory_mib": memory_mib})
        now = self._clock()
        with tx(self._sf) as s:
            replay = self._idempotent_replay(s, idempotency_key, "create", request_hash, now)
            if replay is not None:
                return replay, False
            instance_id = str(uuid.uuid4())
            inst = Instance(
                id=instance_id,
                name=name,
                vcpus=vcpus,
                memory_mib=memory_mib,
                state=VMState.PROVISIONING,
                desired_state=ACTION_DESIRED[OperationAction.CREATE],
                provider_id=f"vm-{instance_id}",
                version=1,
                created_at=now,
                updated_at=now,
            )
            s.add(inst)
            s.flush()
            host = self._scheduler.place(s, instance_id, vcpus, memory_mib)  # may raise -> rollback
            inst.host_id = host.host_id
            audit(s, instance_id, "created", to_state=VMState.PROVISIONING, now=now)
            audit(
                s,
                instance_id,
                "resources_reserved",
                detail=f"{vcpus} vCPU, {memory_mib} MiB on {host.name}",
                now=now,
            )
            op = self._enqueue(s, inst, OperationAction.CREATE, now)
            self._remember(s, idempotency_key, "create", request_hash, op, now)
        log.info("instance_create_accepted", extra={"instance_id": instance_id, "host": host.name})
        return op, True

    def request_action(
        self, instance_id: str, action: OperationAction, idempotency_key: str | None = None
    ) -> tuple[Operation, bool]:
        """start / stop / terminate. Validates the transition and queues the work."""
        if action is OperationAction.CREATE:
            raise ValueError("use create_instance")
        scope = f"{action.value}:{instance_id}"
        now = self._clock()
        with tx(self._sf) as s:
            replay = self._idempotent_replay(s, idempotency_key, scope, "", now)
            if replay is not None:
                return replay, False
            inst = get_instance(s, instance_id)
            transitional, _ = ACTION_STATES[action]
            op_id = str(uuid.uuid4())
            apply_transition(
                s, inst, transitional, action=action.value, operation_id=op_id, now=now
            )
            inst.desired_state = ACTION_DESIRED[action]
            op = self._enqueue(s, inst, action, now, op_id)
            self._remember(s, idempotency_key, scope, "", op, now)
        return op, True

    # ---- reads -----------------------------------------------------------------------------

    def get_instance(self, instance_id: str) -> Instance:
        with read_tx(self._sf) as s:
            return get_instance(s, instance_id)

    def list_instances(
        self, state: VMState | None, limit: int, offset: int
    ) -> tuple[list[Instance], int]:
        with read_tx(self._sf) as s:
            query = select(Instance)
            count = select(func.count()).select_from(Instance)
            if state is not None:
                query = query.where(Instance.state == state)
                count = count.where(Instance.state == state)
            rows = s.scalars(
                query.order_by(Instance.created_at, Instance.id).limit(limit).offset(offset)
            ).all()
            return list(rows), s.scalar(count) or 0

    def get_operation(self, operation_id: str) -> Operation:
        with read_tx(self._sf) as s:
            return get_operation(s, operation_id)

    def list_operations(
        self, instance_id: str, limit: int, offset: int
    ) -> tuple[list[Operation], int]:
        with read_tx(self._sf) as s:
            get_instance(s, instance_id)
            base = select(Operation).where(Operation.instance_id == instance_id)
            rows = s.scalars(
                base.order_by(Operation.created_at, Operation.id).limit(limit).offset(offset)
            ).all()
            total = s.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.instance_id == instance_id)
            )
            return list(rows), total or 0

    def wait_for_operation(self, operation_id: str, timeout_s: float) -> Operation:
        """Poll until the operation reaches a terminal status or the timeout elapses."""
        deadline = time.monotonic() + timeout_s
        while True:
            op = self.get_operation(operation_id)
            if op.status in TERMINAL_OPERATION_STATUSES or time.monotonic() >= deadline:
                return op
            time.sleep(0.02)

    def list_events(
        self, instance_id: str, limit: int, offset: int
    ) -> tuple[list[AuditEvent], int]:
        with read_tx(self._sf) as s:
            get_instance(s, instance_id)
            rows = s.scalars(
                select(AuditEvent)
                .where(AuditEvent.instance_id == instance_id)
                .order_by(AuditEvent.id)
                .limit(limit)
                .offset(offset)
            ).all()
            total = s.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.instance_id == instance_id)
            )
            return list(rows), total or 0

    def capacity(self) -> ClusterCapacity:
        with read_tx(self._sf) as s:
            return self._scheduler.snapshot(s)

    def pending_operations(self) -> int:
        with read_tx(self._sf) as s:
            return (
                s.scalar(
                    select(func.count())
                    .select_from(Operation)
                    .where(Operation.status.in_([OperationStatus.PENDING, OperationStatus.RUNNING]))
                )
                or 0
            )

    def ping_db(self) -> None:
        with read_tx(self._sf) as s:
            s.execute(select(1))

    # ---- internals -------------------------------------------------------------------------

    def _enqueue(
        self,
        s: Session,
        inst: Instance,
        action: OperationAction,
        now: datetime,
        op_id: str | None = None,
    ) -> Operation:
        transitional, final = ACTION_STATES[action]
        op = Operation(
            id=op_id or str(uuid.uuid4()),
            instance_id=inst.id,
            action=action,
            status=OperationStatus.PENDING,
            from_state=transitional,
            target_state=final,
            instance_version=inst.version,
            attempts=0,
            max_attempts=self._max_attempts,
            request_id=request_id_var.get(),
            next_attempt_at=now,
            created_at=now,
        )
        s.add(op)
        s.flush()
        audit(s, inst.id, "operation_queued", detail=action.value, operation_id=op.id, now=now)
        return op

    def _idempotent_replay(
        self, s: Session, key: str | None, scope: str, request_hash: str, now: datetime
    ) -> Operation | None:
        if key is None:
            return None
        existing = s.get(IdempotencyKey, (key, scope))
        if existing is None:
            return None
        if existing.expires_at <= now:
            s.delete(existing)  # expired: the key may be reused
            s.flush()
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                "Idempotency-Key was already used with a different request",
                {"scope": scope},
            )
        return get_operation(s, existing.operation_id)

    def _remember(
        self,
        s: Session,
        key: str | None,
        scope: str,
        request_hash: str,
        op: Operation,
        now: datetime,
    ) -> None:
        if key is None:
            return
        s.add(
            IdempotencyKey(
                key=key,
                scope=scope,
                request_hash=request_hash,
                operation_id=op.id,
                instance_id=op.instance_id,
                expires_at=now + self._idempotency_ttl,
                created_at=now,
            )
        )


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
