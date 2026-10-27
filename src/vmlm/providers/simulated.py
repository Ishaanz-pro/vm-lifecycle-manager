"""Deterministic in-memory provider. SIMULATED: it creates no real VMs and is not a hypervisor.

Failures and delays are injected explicitly (`fail_next`, `set_latency`), and external drift
can be modelled with `set_external_state`, so every test is deterministic. State lives in
process memory and is lost when the process exits (see docs/virtualization.md).
"""

import threading
import time
from typing import Any

from vmlm.providers.base import ProviderError, ProviderVMState

OPERATIONS = frozenset({"provision", "start", "stop", "delete", "inspect"})


class SimulatedProvider:
    def __init__(self) -> None:
        self._vms: dict[str, ProviderVMState] = {}
        self._failures: dict[str, list[bool]] = {}  # op -> queue of `retryable` flags
        self._latency: dict[str, float] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []  # (operation, provider_id), for test assertions

    # ---- test / failure-injection controls ---------------------------------------------

    def fail_next(self, operation: str, times: int = 1, *, retryable: bool = False) -> None:
        self._check_op(operation)
        with self._lock:
            self._failures.setdefault(operation, []).extend([retryable] * times)

    def set_latency(self, operation: str, seconds: float) -> None:
        """Make the next calls of `operation` take `seconds` (0 clears). Used to test timeouts."""
        self._check_op(operation)
        with self._lock:
            if seconds <= 0:
                self._latency.pop(operation, None)
            else:
                self._latency[operation] = seconds

    def set_external_state(self, provider_id: str, state: ProviderVMState | None) -> None:
        """Model something happening outside the control plane (VM crashed, was stopped)."""
        with self._lock:
            if state is None:
                self._vms.pop(provider_id, None)
            else:
                self._vms[provider_id] = state

    def snapshot(self) -> dict[str, ProviderVMState]:
        with self._lock:
            return dict(self._vms)

    def pending_failures(self) -> dict[str, int]:
        with self._lock:
            return {op: len(q) for op, q in self._failures.items() if q}

    # ---- VMProvider ----------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {"name": "simulated", "simulated": True, "real_vms": False}

    def provision(self, provider_id: str, vcpus: int, memory_mib: int) -> None:
        with self._enter("provision", provider_id):
            self._vms.setdefault(provider_id, ProviderVMState.RUNNING)

    def start(self, provider_id: str) -> None:
        with self._enter("start", provider_id):
            self._require(provider_id)
            self._vms[provider_id] = ProviderVMState.RUNNING

    def stop(self, provider_id: str) -> None:
        with self._enter("stop", provider_id):
            self._require(provider_id)
            self._vms[provider_id] = ProviderVMState.STOPPED

    def inspect(self, provider_id: str) -> ProviderVMState | None:
        with self._enter("inspect", provider_id):
            return self._vms.get(provider_id)

    def delete(self, provider_id: str) -> None:
        with self._enter("delete", provider_id):
            self._vms.pop(provider_id, None)

    # ---- internals -----------------------------------------------------------------------

    def _enter(self, operation: str, provider_id: str) -> "_Call":
        return _Call(self, operation, provider_id)

    def _require(self, provider_id: str) -> None:
        if provider_id not in self._vms:
            raise ProviderError(f"no such VM {provider_id!r}")

    @staticmethod
    def _check_op(operation: str) -> None:
        if operation not in OPERATIONS:
            raise ValueError(f"unknown operation {operation!r}")


class _Call:
    """Records the call, applies latency outside the lock, then injected failure inside it."""

    def __init__(self, p: SimulatedProvider, operation: str, provider_id: str) -> None:
        self.p, self.operation, self.provider_id = p, operation, provider_id

    def __enter__(self) -> None:
        p = self.p
        with p._lock:
            p.calls.append((self.operation, self.provider_id))
            delay = p._latency.get(self.operation, 0.0)
        if delay:
            time.sleep(delay)
        p._lock.acquire()
        queue = p._failures.get(self.operation)
        if queue:
            retryable = queue.pop(0)
            p._lock.release()
            raise ProviderError(f"simulated {self.operation} failure", retryable=retryable)

    def __exit__(self, *_: object) -> None:
        self.p._lock.release()
