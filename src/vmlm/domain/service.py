"""Lifecycle service: orchestrates state machine, scheduler, provider and audit log.

Operation shape (create/start/stop/terminate):
  1. Short transaction: validate transition, commit the transitional state (+ reservation,
     audit). Provider is NOT called while a DB write lock is held.
  2. Call the provider (idempotent, keyed by provider_id).
  3. Short transaction: commit the outcome (final state or FAILED), release resources if due.
Each step carries the instance `version` seen in step 1; if another writer changed the row
in between, step 3 raises ConcurrentModification instead of overwriting.

If the process dies between 2 and 3, the row is left in a transitional state and
`reconcile()` resolves it by inspecting the provider.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import AuditEvent, IdempotencyKey, Instance, utcnow
from vmlm.domain.errors import (
    ConcurrentModification,
    DomainError,
    IdempotencyConflict,
    InstanceNotFound,
    ProviderOperationFailed,
)
from vmlm.domain.scheduler import CapacitySnapshot, Scheduler
from vmlm.domain.states import TRANSITIONAL_STATES, VMState, assert_transition
from vmlm.providers.base import ProviderError, ProviderVMState, VMProvider

log = logging.getLogger("vmlm.service")


class LifecycleService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: VMProvider,
        scheduler: Scheduler,
    ) -> None:
        self._sf = session_factory
        self._provider = provider
        self._scheduler = scheduler

    # ---- public API -------------------------------------------------------------------

    def create_instance(
        self, name: str, vcpus: int, memory_mib: int, idempotency_key: str | None = None
    ) -> tuple[Instance, bool]:
        """Returns (instance, created). created=False means an idempotent replay."""
        request_hash = _hash_request(name, vcpus, memory_mib)
        with self._tx() as s:
            if idempotency_key is not None:
                existing = s.get(IdempotencyKey, idempotency_key)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used with a different request"
                        )
                    return self._get(s, existing.instance_id), False
            instance_id = str(uuid.uuid4())
            inst = Instance(
                id=instance_id,
                name=name,
                vcpus=vcpus,
                memory_mib=memory_mib,
                state=VMState.PROVISIONING,
                provider_id=f"vm-{instance_id}",
                version=1,
            )
            s.add(inst)
            s.flush()
            self._scheduler.reserve(s, instance_id, vcpus, memory_mib)  # may raise -> rollback
            self._audit(s, instance_id, "created", to_state=VMState.PROVISIONING)
            self._audit(
                s, instance_id, "resources_reserved", detail=f"{vcpus} vCPU, {memory_mib} MiB"
            )
            if idempotency_key is not None:
                s.add(
                    IdempotencyKey(
                        key=idempotency_key, request_hash=request_hash, instance_id=instance_id
                    )
                )
            version, provider_id = inst.version, inst.provider_id
        log.info("instance_created", extra={"instance_id": instance_id})

        try:
            self._provider.provision(provider_id, vcpus, memory_mib)
        except ProviderError as exc:
            # Nothing usable was created: roll back the reservation.
            self._best_effort_cleanup(provider_id)
            failed = self._fail(instance_id, version, f"provision failed: {exc}", release=True)
            raise _provider_failure("provision", exc, failed) from exc
        return self._complete(instance_id, version, VMState.RUNNING), True

    def start_instance(self, instance_id: str) -> Instance:
        return self._operate(
            instance_id, "start", VMState.STARTING, VMState.RUNNING, self._provider.start
        )

    def stop_instance(self, instance_id: str) -> Instance:
        return self._operate(
            instance_id, "stop", VMState.STOPPING, VMState.STOPPED, self._provider.stop
        )

    def terminate_instance(self, instance_id: str) -> Instance:
        """Destroy at the provider, release resources, keep the row as TERMINATED (audit)."""
        return self._operate(
            instance_id,
            "terminate",
            VMState.TERMINATING,
            VMState.TERMINATED,
            self._provider.delete,
            release=True,
        )

    def get_instance(self, instance_id: str) -> Instance:
        with self._tx() as s:
            return self._get(s, instance_id)

    def list_instances(
        self, state: VMState | None, limit: int, offset: int
    ) -> tuple[list[Instance], int]:
        with self._tx() as s:
            query = select(Instance)
            count = select(func.count()).select_from(Instance)
            if state is not None:
                query = query.where(Instance.state == state)
                count = count.where(Instance.state == state)
            rows = s.scalars(
                query.order_by(Instance.created_at, Instance.id).limit(limit).offset(offset)
            ).all()
            return list(rows), s.scalar(count) or 0

    def list_events(
        self, instance_id: str, limit: int, offset: int
    ) -> tuple[list[AuditEvent], int]:
        with self._tx() as s:
            self._get(s, instance_id)
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

    def capacity(self) -> CapacitySnapshot:
        with self._tx() as s:
            return self._scheduler.snapshot(s)

    def ping_db(self) -> None:
        with self._tx() as s:
            s.execute(select(1))

    # ---- reconciliation ---------------------------------------------------------------

    def reconcile(self) -> int:
        """Resolve instances stuck in a transitional state by asking the provider.

        Safe to run at startup. Every change goes through the version check, so it cannot
        clobber an operation that is still in flight. Returns the number resolved.
        Steady-state drift (e.g. a RUNNING row whose VM vanished) is NOT detected.
        """
        with self._tx() as s:
            stuck = [
                (i.id, i.version, i.state, i.provider_id)
                for i in s.scalars(select(Instance).where(Instance.state.in_(TRANSITIONAL_STATES)))
            ]
        resolved = 0
        for instance_id, version, state, provider_id in stuck:
            try:
                self._reconcile_one(instance_id, version, state, provider_id)
                resolved += 1
            except (DomainError, ProviderError) as exc:
                log.warning(
                    "reconcile_skipped", extra={"instance_id": instance_id, "reason": str(exc)}
                )
        return resolved

    def _reconcile_one(
        self, instance_id: str, version: int, state: VMState, provider_id: str
    ) -> None:
        observed = self._provider.inspect(provider_id)
        note = f"reconciled from {state}; provider reports {observed}"
        if state is VMState.PROVISIONING:
            if observed is ProviderVMState.RUNNING:
                self._complete(instance_id, version, VMState.RUNNING, detail=note)
            else:
                self._fail(instance_id, version, "provisioning did not complete", True, note)
        elif state is VMState.STARTING:
            if observed is ProviderVMState.RUNNING:
                self._complete(instance_id, version, VMState.RUNNING, detail=note)
            else:
                self._fail(instance_id, version, "start did not complete", detail=note)
        elif state is VMState.STOPPING:
            if observed is ProviderVMState.STOPPED:
                self._complete(instance_id, version, VMState.STOPPED, detail=note)
            else:
                self._fail(instance_id, version, "stop did not complete", detail=note)
        elif state is VMState.TERMINATING:
            if observed is not None:
                self._provider.delete(provider_id)  # idempotent retry
            self._complete(instance_id, version, VMState.TERMINATED, detail=note, release=True)

    # ---- internals --------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        with self._sf() as session, session.begin():
            yield session

    @staticmethod
    def _get(s: Session, instance_id: str) -> Instance:
        inst = s.get(Instance, instance_id)
        if inst is None:
            raise InstanceNotFound(
                f"instance {instance_id} not found", {"instance_id": instance_id}
            )
        return inst

    @staticmethod
    def _audit(
        s: Session,
        instance_id: str,
        event_type: str,
        from_state: VMState | None = None,
        to_state: VMState | None = None,
        detail: str | None = None,
    ) -> None:
        s.add(
            AuditEvent(
                instance_id=instance_id,
                event_type=event_type,
                from_state=from_state.value if from_state else None,
                to_state=to_state.value if to_state else None,
                detail=detail,
            )
        )

    def _apply(
        self,
        s: Session,
        inst: Instance,
        target: VMState,
        *,
        action: str | None = None,
        expected_version: int | None = None,
        detail: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if expected_version is not None and inst.version != expected_version:
            raise ConcurrentModification(
                f"instance {inst.id} was modified concurrently", {"instance_id": inst.id}
            )
        assert_transition(inst.state, target, action)
        previous = inst.state
        inst.state = target
        inst.version += 1
        inst.updated_at = utcnow()
        if failure_reason is not None:
            inst.failure_reason = failure_reason
        self._audit(s, inst.id, "state_changed", previous, target, detail or failure_reason)
        log.info(
            "state_changed",
            extra={"instance_id": inst.id, "from_state": previous, "to_state": target},
        )

    def _complete(
        self,
        instance_id: str,
        version: int,
        target: VMState,
        detail: str | None = None,
        release: bool = False,
    ) -> Instance:
        with self._tx() as s:
            inst = self._get(s, instance_id)
            self._apply(s, inst, target, expected_version=version, detail=detail)
            if release and self._scheduler.release(s, instance_id):
                self._audit(s, instance_id, "resources_released")
            return inst

    def _fail(
        self,
        instance_id: str,
        version: int,
        reason: str,
        release: bool = False,
        detail: str | None = None,
    ) -> Instance:
        with self._tx() as s:
            inst = self._get(s, instance_id)
            self._apply(
                s,
                inst,
                VMState.FAILED,
                expected_version=version,
                detail=detail,
                failure_reason=reason,
            )
            if release and self._scheduler.release(s, instance_id):
                self._audit(s, instance_id, "resources_released", detail="provisioning rolled back")
            return inst

    def _operate(
        self,
        instance_id: str,
        action: str,
        transitional: VMState,
        final: VMState,
        call: Callable[[str], None],
        release: bool = False,
    ) -> Instance:
        with self._tx() as s:
            inst = self._get(s, instance_id)
            self._apply(s, inst, transitional, action=action)
            version, provider_id = inst.version, inst.provider_id
        try:
            call(provider_id)
        except ProviderError as exc:
            failed = self._fail(instance_id, version, f"{action} failed: {exc}")
            raise _provider_failure(action, exc, failed) from exc
        return self._complete(instance_id, version, final, release=release)

    def _best_effort_cleanup(self, provider_id: str) -> None:
        try:
            self._provider.delete(provider_id)
        except ProviderError as exc:
            log.warning("cleanup_failed", extra={"provider_id": provider_id, "reason": str(exc)})


def _hash_request(name: str, vcpus: int, memory_mib: int) -> str:
    canonical = json.dumps({"name": name, "vcpus": vcpus, "memory_mib": memory_mib}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _provider_failure(action: str, exc: ProviderError, inst: Instance) -> ProviderOperationFailed:
    return ProviderOperationFailed(
        f"provider {action} failed: {exc}",
        {"instance_id": inst.id, "state": inst.state.value},
    )
