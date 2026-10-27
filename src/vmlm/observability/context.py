"""Request/operation correlation IDs carried through contextvars into every log line."""

import contextvars
import uuid

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
operation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "operation_id", default=None
)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
