"""Uniform error format: {"error": {"code", "message", "details", "request_id"}}.

Unhandled exceptions are logged with their traceback (with the request id) and returned as
a generic 500 by the request-context middleware; stack traces and host details never reach
the client.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from vmlm.domain import errors as e
from vmlm.observability.context import request_id_var

STATUS_BY_ERROR: dict[type[e.DomainError], int] = {
    e.InstanceNotFound: 404,
    e.OperationNotFound: 404,
    e.HostNotFound: 404,
    e.InvalidTransition: 409,
    e.InsufficientCapacity: 409,
    e.DoubleAllocation: 409,
    e.ConcurrentModification: 409,
    e.IdempotencyConflict: 409,
    e.Unauthorized: 401,
    e.Forbidden: 403,
    e.FeatureDisabled: 403,
}


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "details": details or {}}}
    if (rid := request_id_var.get()) is not None:
        body["error"]["request_id"] = rid
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(e.DomainError)
    async def _domain(_: Request, exc: e.DomainError) -> JSONResponse:
        status = STATUS_BY_ERROR.get(type(exc), 400)
        headers = {"WWW-Authenticate": "ApiKey"} if status == 401 else None
        return JSONResponse(_body(exc.code, exc.message, exc.details), status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            _body(
                "validation_error",
                "request validation failed",
                {"errors": jsonable_encoder(errors)},
            ),
            422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(_body("http_error", str(exc.detail)), exc.status_code)

    # Unhandled exceptions are turned into a generic 500 by RequestContextMiddleware, which
    # still has the request id in scope; a handler registered here would run outside it.
