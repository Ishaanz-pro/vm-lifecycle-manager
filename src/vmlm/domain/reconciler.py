"""Reconciler: compares desired state with what the provider actually reports, and repairs.

Runs at startup and every `reconcile_interval_s` (also on demand via the admin API). It is
the only component allowed to change an instance's state based on *observation* rather than
a user request, and it does so through the explicit OBSERVED_CORRECTIONS table below.

Pass 1  requeue operations whose worker lease expired (crash / DB error mid-operation).
Pass 2  mark agent-managed hosts UNHEALTHY when their heartbeat is stale.
Pass 3  drift: for every instance in a stable state (RUNNING/STOPPED) with no operation in
        flight, inspect the provider and
          observed matches          -> nothing
          observed differs          -> correct the recorded state, then queue an operation to
                                       restore the desired state (e.g. RUNNING desired, VM
                                       found STOPPED -> record STOPPED, queue START)
          VM missing at provider    -> FAILED ("vm missing at provider"); capacity is kept
                                       until the user terminates. We never silently re-create
                                       a VM: that could duplicate a workload that still exists
                                       somewhere we cannot see.
Instances with an operation in flight are left alone: the worker owns them.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import Host, HostStatus, Instance, Operation, utcnow
from vmlm.domain.core import Clock, audit, read_tx, tx
from vmlm.domain.errors import DomainError
from vmlm.domain.service import LifecycleService
from vmlm.domain.states import (
    DesiredState,
    OperationAction,
    OperationStatus,
    VMState,
)
from vmlm.domain.worker import Worker
from vmlm.observability.context import request_id_var
from vmlm.observability.metrics import HOST_CAPACITY, OPERATIONS_QUEUED, RECONCILE_REPAIRS
from vmlm.providers.base import ProviderError, ProviderVMState, VMProvider

log = logging.getLogger("vmlm.reconciler")

# (recorded state, observed provider state) -> corrected recorded state
OBSERVED_CORRECTIONS: dict[tuple[VMState, ProviderVMState | None], VMState] = {
    (VMState.RUNNING, ProviderVMState.STOPPED): VMState.STOPPED,
    (VMState.STOPPED, ProviderVMState.RUNNING): VMState.RUNNING,
    (VMState.RUNNING, None): VMState.FAILED,
    (VMState.STOPPED, None): VMState.FAILED,
}

RESTORE_ACTION: dict[tuple[VMState, DesiredState], OperationAction] = {
    (VMState.STOPPED, DesiredState.RUNNING): OperationAction.START,
    (VMState.RUNNING, DesiredState.STOPPED): OperationAction.STOP,
}


@dataclass
class ReconcileReport:
    requeued_operations: int = 0
    hosts_marked_unhealthy: int = 0
    instances_inspected: int = 0
    drift_corrected: int = 0
    restore_operations_queued: int = 0
    marked_failed: int = 0
    skipped_provider_errors: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class Reconciler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: VMProvider,
        service: LifecycleService,
        worker: Worker,
        *,
        heartbeat_stale_s: float = 60.0,
        clock: Clock = utcnow,
    ) -> None:
        self._sf = session_factory
        self._provider = provider
        self._service = service
        self._worker = worker
        self._stale = timedelta(seconds=heartbeat_stale_s)
        self._clock = clock

    def run_once(self) -> ReconcileReport:
        token = request_id_var.set("reconciler")
        try:
            report = ReconcileReport()
            report.requeued_operations = self._worker.requeue_expired_leases()
            report.hosts_marked_unhealthy = self._check_heartbeats()
            self._check_drift(report)
            self._publish_gauges()
            if report.requeued_operations or report.drift_corrected or report.marked_failed:
                log.info("reconcile_done", extra=report.as_dict())
            return report
        finally:
            request_id_var.reset(token)

    # ---- passes ------------------------------------------------------------------------------

    def _check_heartbeats(self) -> int:
        now = self._clock()
        marked = 0
        with tx(self._sf) as s:
            for host in s.scalars(
                select(Host).where(Host.agent_managed.is_(True), Host.status == HostStatus.READY)
            ):
                if host.last_heartbeat_at is None or now - host.last_heartbeat_at > self._stale:
                    host.status = HostStatus.UNHEALTHY
                    host.updated_at = now
                    marked += 1
                    RECONCILE_REPAIRS.inc(kind="host_unhealthy")
                    log.warning("host_heartbeat_stale", extra={"host": host.name})
        return marked

    def _check_drift(self, report: ReconcileReport) -> None:
        with read_tx(self._sf) as s:
            in_flight = exists().where(
                Operation.instance_id == Instance.id,
                Operation.status.in_([OperationStatus.PENDING, OperationStatus.RUNNING]),
            )
            candidates = [
                (i.id, i.version, i.state, i.desired_state, i.provider_id)
                for i in s.scalars(
                    select(Instance).where(
                        Instance.state.in_([VMState.RUNNING, VMState.STOPPED]), ~in_flight
                    )
                )
            ]
        for instance_id, version, state, desired, provider_id in candidates:
            report.instances_inspected += 1
            try:
                observed = self._provider.inspect(provider_id)
            except ProviderError as exc:
                report.skipped_provider_errors += 1
                log.warning(
                    "reconcile_inspect_failed",
                    extra={"instance_id": instance_id, "error": str(exc)},
                )
                continue
            corrected = OBSERVED_CORRECTIONS.get((state, observed))
            if corrected is None:
                continue
            try:
                self._correct(instance_id, version, state, corrected, observed, desired, report)
            except DomainError as exc:  # changed under us; next run will look again
                report.errors.append(f"{instance_id}: {exc}")

    def _correct(
        self,
        instance_id: str,
        version: int,
        state: VMState,
        corrected: VMState,
        observed: ProviderVMState | None,
        desired: DesiredState,
        report: ReconcileReport,
    ) -> None:
        now = self._clock()
        note = f"drift: recorded {state}, provider reports {observed}"
        with tx(self._sf) as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.version != version:
                return
            audit(
                s,
                instance_id,
                "drift_detected",
                from_state=state,
                to_state=corrected,
                detail=note,
                now=now,
            )
            inst.state = corrected
            inst.version += 1
            inst.updated_at = now
            if corrected is VMState.FAILED:
                inst.failure_reason = "vm missing at provider"
                report.marked_failed += 1
                RECONCILE_REPAIRS.inc(kind="marked_failed")
            else:
                report.drift_corrected += 1
                RECONCILE_REPAIRS.inc(kind="state_corrected")
        log.warning("drift_corrected", extra={"instance_id": instance_id, "detail": note})
        action = RESTORE_ACTION.get((corrected, desired))
        if action is not None:
            self._service.request_action(instance_id, action)
            report.restore_operations_queued += 1
            RECONCILE_REPAIRS.inc(kind=f"restore_{action.value}")

    def _publish_gauges(self) -> None:
        snap = self._service.capacity()
        for h in snap.hosts:
            HOST_CAPACITY.set(h.total_vcpus, host=h.name, resource="vcpus", kind="total")
            HOST_CAPACITY.set(h.reserved_vcpus, host=h.name, resource="vcpus", kind="reserved")
            HOST_CAPACITY.set(h.total_memory_mib, host=h.name, resource="memory_mib", kind="total")
            HOST_CAPACITY.set(
                h.reserved_memory_mib, host=h.name, resource="memory_mib", kind="reserved"
            )
        OPERATIONS_QUEUED.set(self._service.pending_operations())
