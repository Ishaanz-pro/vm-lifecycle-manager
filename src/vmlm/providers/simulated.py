"""Deterministic in-memory provider. SIMULATED: it creates no real VMs and is not a hypervisor.

Failures are injected explicitly (`fail_next`) so tests are deterministic. State lives in
process memory, so it is lost when the process exits (see README: Known limitations).
"""

import threading

from vmlm.providers.base import ProviderError, ProviderVMState

OPERATIONS = frozenset({"provision", "start", "stop", "delete", "inspect"})


class SimulatedProvider:
    def __init__(self) -> None:
        self._vms: dict[str, ProviderVMState] = {}
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []  # (operation, provider_id), for test assertions

    def fail_next(self, operation: str, times: int = 1) -> None:
        if operation not in OPERATIONS:
            raise ValueError(f"unknown operation {operation!r}")
        with self._lock:
            self._failures[operation] = times

    def snapshot(self) -> dict[str, ProviderVMState]:
        with self._lock:
            return dict(self._vms)

    def _enter(self, operation: str, provider_id: str) -> None:
        self.calls.append((operation, provider_id))
        remaining = self._failures.get(operation, 0)
        if remaining > 0:
            self._failures[operation] = remaining - 1
            raise ProviderError(f"simulated {operation} failure")

    def provision(self, provider_id: str, vcpus: int, memory_mib: int) -> None:
        with self._lock:
            self._enter("provision", provider_id)
            self._vms.setdefault(provider_id, ProviderVMState.RUNNING)

    def start(self, provider_id: str) -> None:
        with self._lock:
            self._enter("start", provider_id)
            self._require(provider_id)
            self._vms[provider_id] = ProviderVMState.RUNNING

    def stop(self, provider_id: str) -> None:
        with self._lock:
            self._enter("stop", provider_id)
            self._require(provider_id)
            self._vms[provider_id] = ProviderVMState.STOPPED

    def inspect(self, provider_id: str) -> ProviderVMState | None:
        with self._lock:
            self._enter("inspect", provider_id)
            return self._vms.get(provider_id)

    def delete(self, provider_id: str) -> None:
        with self._lock:
            self._enter("delete", provider_id)
            self._vms.pop(provider_id, None)

    def _require(self, provider_id: str) -> None:
        if provider_id not in self._vms:
            raise ProviderError(f"no such VM {provider_id!r}")
