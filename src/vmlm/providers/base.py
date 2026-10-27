"""Provider interface.

All operations are keyed by a provider_id chosen by the control plane (a client token), which
makes every call safe to retry: provisioning an existing id, or deleting a missing one, is
a no-op. The worker depends on that property when it retries after a timeout or a crash.

Errors are classified so the worker can decide between retrying and failing:
  ProviderError(retryable=True)   transient: retry with backoff (network blip, busy host)
  ProviderError(retryable=False)  permanent: fail the operation (bad image, unsupported request)
  ProviderTimeout                 the call exceeded its deadline; transient. The call may still
                                  complete in the background, which is why calls are idempotent.
"""

from enum import StrEnum
from typing import Any, Protocol


class ProviderError(Exception):
    """Raised by a provider when an operation fails."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable

    @property
    def kind(self) -> str:
        return "transient" if self.retryable else "permanent"


class ProviderTimeout(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderUnavailable(RuntimeError):
    """Raised at startup when a provider's prerequisites are missing. Never falls back."""


class ProviderVMState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class VMProvider(Protocol):
    def describe(self) -> dict[str, Any]:
        """Static facts for /health and docs: name, whether it is simulated, capabilities."""

    def provision(self, provider_id: str, vcpus: int, memory_mib: int) -> None:
        """Create and boot a VM. Idempotent for an existing provider_id."""

    def start(self, provider_id: str) -> None: ...

    def stop(self, provider_id: str) -> None: ...

    def inspect(self, provider_id: str) -> ProviderVMState | None:
        """Return the VM's state, or None if the provider has no such VM."""

    def delete(self, provider_id: str) -> None:
        """Destroy the VM. Deleting a VM that does not exist is not an error."""
