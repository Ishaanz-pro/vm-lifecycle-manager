"""Domain errors. They carry a stable machine-readable `code`; the API layer maps them to HTTP."""

from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InstanceNotFound(DomainError):
    code = "instance_not_found"


class OperationNotFound(DomainError):
    code = "operation_not_found"


class HostNotFound(DomainError):
    code = "host_not_found"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class InsufficientCapacity(DomainError):
    code = "insufficient_capacity"


class DoubleAllocation(DomainError):
    code = "double_allocation"


class ConcurrentModification(DomainError):
    code = "concurrent_modification"


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"


class Unauthorized(DomainError):
    code = "unauthorized"


class Forbidden(DomainError):
    code = "forbidden"


class FeatureDisabled(DomainError):
    code = "feature_disabled"
