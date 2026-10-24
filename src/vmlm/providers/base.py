"""Provider interface. All operations are keyed by a provider_id chosen by the control plane
(a client token), which makes provision/delete safe to retry: repeating a call is harmless."""

from enum import StrEnum
from typing import Protocol


class ProviderError(Exception):
    """Raised by a provider when an operation fails."""


class ProviderVMState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class VMProvider(Protocol):
    def provision(self, provider_id: str, vcpus: int, memory_mib: int) -> None:
        """Create and boot a VM. Idempotent for an existing provider_id."""

    def start(self, provider_id: str) -> None: ...

    def stop(self, provider_id: str) -> None: ...

    def inspect(self, provider_id: str) -> ProviderVMState | None:
        """Return the VM's state, or None if the provider has no such VM."""

    def delete(self, provider_id: str) -> None:
        """Destroy the VM. Deleting a VM that does not exist is not an error."""
