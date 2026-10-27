"""Development-only failure injection.

Provider-level failures (errors, timeouts, delays) are injected on the SimulatedProvider.
This object covers failures *inside the control plane*: a worker crashing between the
provider call and the outcome commit, and the outcome commit itself failing. Both leave the
operation claimed under its lease; the test and docs show the lease expiring and the work
being picked up again (docs/reliability.md).

Disabled unless VMLM_FAILURE_INJECTION_ENABLED=true; the debug API refuses otherwise.
"""

import threading
from dataclasses import dataclass, field


class SimulatedWorkerCrash(RuntimeError):
    pass


class SimulatedDatabaseError(RuntimeError):
    pass


@dataclass
class FailureInjector:
    enabled: bool = False
    crash_worker_next: int = 0
    db_error_next: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def arm(self, kind: str, times: int = 1) -> None:
        with self._lock:
            if kind == "worker_crash":
                self.crash_worker_next += times
            elif kind == "db_error":
                self.db_error_next += times
            else:
                raise ValueError(f"unknown failure kind {kind!r}")

    def take(self, kind: str) -> bool:
        """Consume one armed failure of `kind`, returning whether one was armed."""
        with self._lock:
            attr = {"worker_crash": "crash_worker_next", "db_error": "db_error_next"}[kind]
            remaining = getattr(self, attr)
            if remaining <= 0:
                return False
            setattr(self, attr, remaining - 1)
            return True

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"worker_crash": self.crash_worker_next, "db_error": self.db_error_next}
