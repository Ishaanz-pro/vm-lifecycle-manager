"""Operation worker: claims PENDING operations under a lease, calls the provider with a
deadline, and commits the outcome.

Claiming is one atomic transaction (BEGIN IMMEDIATE on SQLite, SELECT ... FOR UPDATE SKIP
LOCKED on PostgreSQL), so N workers on one database never execute the same operation
concurrently. A claimed operation carries `claimed_by` and `lease_expires_at`; if the worker
dies, the lease expires and the next `requeue_expired_leases()` (run by the reconciler and at
startup) makes the work claimable again. Re-running is safe because provider calls are
idempotent for a given provider_id.

Outcome rules (docs/reliability.md):
  success                       -> instance to target state; terminate releases capacity
  transient error, attempts<max -> operation back to PENDING at now + backoff(jittered)
  permanent error or exhausted  -> instance FAILED; create releases capacity (rollback)
  worker crash / DB error       -> nothing committed; lease expiry re-queues the operation

Provider calls run on a small thread pool with a deadline. A call that overruns is reported as
ProviderTimeout (transient); the thread finishes in the background. That is at-least-once
execution, not exactly-once, and the docs say so.
"""

import logging
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import Operation, utcnow
from vmlm.domain.core import Clock, apply_transition, audit, get_instance, read_tx, tx
from vmlm.domain.errors import ConcurrentModification, DomainError
from vmlm.domain.failures import FailureInjector, SimulatedDatabaseError, SimulatedWorkerCrash
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.states import OperationAction, OperationStatus, VMState
from vmlm.observability.context import operation_id_var, request_id_var
from vmlm.observability.metrics import (
    OPERATION_ATTEMPTS,
    OPERATION_DURATION,
    OPERATIONS,
    PROVIDER_FAILURES,
)
from vmlm.providers.base import ProviderError, ProviderTimeout, VMProvider

log = logging.getLogger("vmlm.worker")


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: VMProvider,
        scheduler: Scheduler,
        *,
        worker_id: str | None = None,
        lease_s: float = 30.0,
        provider_timeout_s: float = 10.0,
        retry_base_delay_s: float = 0.5,
        retry_max_delay_s: float = 10.0,
        clock: Clock = utcnow,
        injector: FailureInjector | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._sf = session_factory
        self._provider = provider
        self._scheduler = scheduler
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._lease = timedelta(seconds=lease_s)
        self._timeout = provider_timeout_s
        self._base_delay = retry_base_delay_s
        self._max_delay = retry_max_delay_s
        self._clock = clock
        self._injector = injector or FailureInjector()
        self._rng = rng or random.Random()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="provider")
        self.in_flight = 0

    # ---- loop ------------------------------------------------------------------------------

    def run_forever(self, stop: threading.Event, poll_interval_s: float) -> None:
        log.info("worker_started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(poll_interval_s)
            except Exception:  # noqa: BLE001 - a loop must survive anything
                log.exception("worker_iteration_failed")
                stop.wait(poll_interval_s)
        log.info("worker_stopped", extra={"worker_id": self.worker_id})

    def run_once(self) -> bool:
        """Claim and execute at most one operation. Returns True if any work was done."""
        op = self.claim_next()
        if op is None:
            return False
        self.in_flight += 1
        try:
            self.execute(op)
        finally:
            self.in_flight -= 1
        return True

    def run_until_idle(self, max_operations: int = 10_000) -> int:
        """Deterministic drain for tests and the `wait` API: no sleeping between operations."""
        done = 0
        while done < max_operations and self.run_once():
            done += 1
        return done

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- claim / lease -----------------------------------------------------------------------

    def claim_next(self) -> Operation | None:
        now = self._clock()
        is_due = (Operation.status == OperationStatus.PENDING, Operation.next_attempt_at <= now)
        # A read-only "peek" before taking the lock was tried and measured: no tail-latency
        # gain and ~7% lower throughput under load (docs/design-decisions.md), so it was removed.
        with tx(self._sf) as s:
            query = (
                select(Operation)
                .where(*is_due)
                .order_by(Operation.next_attempt_at, Operation.created_at)
                .limit(1)
            )
            if s.bind is not None and s.bind.dialect.name != "sqlite":
                query = query.with_for_update(skip_locked=True)
            op = s.scalar(query)
            if op is None:
                return None
            op.status = OperationStatus.RUNNING
            op.claimed_by = self.worker_id
            op.lease_expires_at = now + self._lease
            op.attempts += 1
            op.started_at = op.started_at or now
            s.flush()
            return op

    def requeue_expired_leases(self) -> int:
        """Make operations whose worker went silent claimable again. Returns the count."""
        now = self._clock()
        with tx(self._sf) as s:
            expired = s.scalars(
                select(Operation).where(
                    Operation.status == OperationStatus.RUNNING,
                    Operation.lease_expires_at < now,
                )
            ).all()
            for op in expired:
                if op.attempts >= op.max_attempts:
                    self._fail_in_session(
                        s, op, "lease expired after the last permitted attempt", "transient", now
                    )
                    continue
                op.status = OperationStatus.PENDING
                op.next_attempt_at = now
                op.error = f"lease expired (worker {op.claimed_by} did not report an outcome)"
                op.error_kind = "transient"
                op.claimed_by = None
                op.lease_expires_at = None
                audit(
                    s,
                    op.instance_id,
                    "operation_requeued",
                    detail="lease expired",
                    operation_id=op.id,
                    now=now,
                )
                log.warning("operation_lease_expired", extra={"op": op.id, "worker": op.claimed_by})
            return len(expired)

    # ---- execute -----------------------------------------------------------------------------

    def execute(self, op: Operation) -> None:
        token_op = operation_id_var.set(op.id)
        token_req = request_id_var.set(op.request_id)
        try:
            self._execute(op)
        finally:
            operation_id_var.reset(token_op)
            request_id_var.reset(token_req)

    def _execute(self, op: Operation) -> None:
        OPERATION_ATTEMPTS.inc(action=op.action.value)
        with read_tx(self._sf) as s:
            inst = get_instance(s, op.instance_id)
            provider_id, vcpus, memory_mib = inst.provider_id, inst.vcpus, inst.memory_mib
        call = self._provider_call(op.action, provider_id, vcpus, memory_mib)

        started = time.monotonic()
        try:
            self._call_with_deadline(call, op.action.value)
        except ProviderError as exc:
            PROVIDER_FAILURES.inc(operation=op.action.value, kind=exc.kind)
            log.warning(
                "provider_call_failed",
                extra={"action": op.action.value, "kind": exc.kind, "error": str(exc)},
            )
            if exc.retryable and op.attempts < op.max_attempts:
                self._retry(op, exc)
            else:
                self._fail(op, str(exc), exc.kind)
            return
        finally:
            log.debug(
                "provider_call_done",
                extra={"action": op.action.value, "seconds": time.monotonic() - started},
            )

        if self._injector.take("worker_crash"):
            # A real crash never reaches the outcome commit; model that precisely.
            raise SimulatedWorkerCrash(f"injected crash before committing {op.id}")
        try:
            self._complete(op)
        except SimulatedDatabaseError:
            log.error("outcome_commit_failed")
            # Operation stays RUNNING under its lease; lease expiry re-queues it.
        except ConcurrentModification as exc:
            log.error("stale_outcome_dropped", extra={"reason": str(exc)})

    def _provider_call(
        self, action: OperationAction, provider_id: str, vcpus: int, memory_mib: int
    ) -> Callable[[], None]:
        p = self._provider
        if action is OperationAction.CREATE:
            return lambda: p.provision(provider_id, vcpus, memory_mib)
        if action is OperationAction.START:
            return lambda: p.start(provider_id)
        if action is OperationAction.STOP:
            return lambda: p.stop(provider_id)
        return lambda: p.delete(provider_id)

    def _call_with_deadline(self, call: Callable[[], None], action: str) -> None:
        future: Future[None] = self._pool.submit(call)
        try:
            future.result(timeout=self._timeout)
        except FutureTimeout:
            raise ProviderTimeout(f"provider {action} exceeded {self._timeout}s") from None

    # ---- outcomes ----------------------------------------------------------------------------

    def _load_claimed(self, s: Session, op_id: str) -> Operation:
        """Re-read the operation and verify this worker still owns it."""
        op = s.get(Operation, op_id)
        if (
            op is None
            or op.status is not OperationStatus.RUNNING
            or op.claimed_by != self.worker_id
        ):
            raise ConcurrentModification(
                "operation is no longer owned by this worker", {"operation_id": op_id}
            )
        return op

    def _complete(self, op_ref: Operation) -> None:
        now = self._clock()
        with tx(self._sf) as s:
            op = self._load_claimed(s, op_ref.id)
            inst = get_instance(s, op.instance_id)
            apply_transition(
                s,
                inst,
                op.target_state,
                expected_version=op.instance_version,
                operation_id=op.id,
                now=now,
            )
            if op.action is OperationAction.TERMINATE and self._scheduler.release(s, inst.id):
                audit(s, inst.id, "resources_released", operation_id=op.id, now=now)
            op.status = OperationStatus.SUCCEEDED
            op.completed_at = now
            op.error = None
            op.error_kind = None
            op.lease_expires_at = None
            if self._injector.take("db_error"):
                raise SimulatedDatabaseError("injected failure before commit")
        self._observe(op, "succeeded", now)

    def _retry(self, op_ref: Operation, exc: ProviderError) -> None:
        now = self._clock()
        delay = self._backoff(op_ref.attempts)
        with tx(self._sf) as s:
            op = self._load_claimed(s, op_ref.id)
            op.status = OperationStatus.PENDING
            op.next_attempt_at = now + timedelta(seconds=delay)
            op.error = str(exc)
            op.error_kind = exc.kind
            op.claimed_by = None
            op.lease_expires_at = None
            audit(
                s,
                op.instance_id,
                "operation_retry_scheduled",
                detail=(
                    f"attempt {op.attempts}/{op.max_attempts} failed: {exc}; retry in {delay:.2f}s"
                ),
                operation_id=op.id,
                now=now,
            )
        log.info("operation_retry_scheduled", extra={"attempt": op_ref.attempts, "delay_s": delay})

    def _fail(self, op_ref: Operation, reason: str, kind: str) -> None:
        now = self._clock()
        with tx(self._sf) as s:
            op = self._load_claimed(s, op_ref.id)
            self._fail_in_session(s, op, reason, kind, now)
        self._observe(op_ref, "failed", now)

    def _fail_in_session(
        self, s: Session, op: Operation, reason: str, kind: str, now: datetime
    ) -> None:
        inst = get_instance(s, op.instance_id)
        message = f"{op.action.value} failed: {reason}"
        try:
            apply_transition(
                s,
                inst,
                VMState.FAILED,
                expected_version=op.instance_version,
                failure_reason=message,
                operation_id=op.id,
                now=now,
            )
        except DomainError as exc:  # instance moved on without us; record and give up
            log.error("fail_transition_rejected", extra={"op": op.id, "reason": str(exc)})
        if op.action is OperationAction.CREATE and self._scheduler.release(s, inst.id):
            audit(
                s,
                inst.id,
                "resources_released",
                detail="provisioning rolled back",
                operation_id=op.id,
                now=now,
            )
        op.status = OperationStatus.FAILED
        op.completed_at = now
        op.error = reason
        op.error_kind = kind
        op.lease_expires_at = None

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped: U(0, min(max, base * 2^(attempt-1)))."""
        ceiling = min(self._max_delay, self._base_delay * (2 ** max(attempt - 1, 0)))
        return self._rng.uniform(0, ceiling) if ceiling > 0 else 0.0

    @staticmethod
    def _observe(op: Operation, outcome: str, now: datetime) -> None:
        OPERATIONS.inc(action=op.action.value, outcome=outcome)
        OPERATION_DURATION.observe((now - op.created_at).total_seconds(), action=op.action.value)
