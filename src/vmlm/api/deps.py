"""Dependency-injection helpers. The service and settings live on app.state."""

import secrets
from typing import Annotated

from fastapi import Header, Request

from vmlm.domain.errors import Unauthorized
from vmlm.domain.service import LifecycleService


def get_service(request: Request) -> LifecycleService:
    return request.app.state.service  # type: ignore[no-any-return]


def require_api_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected: str | None = request.app.state.settings.api_key
    if expected is None:
        return  # auth disabled
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise Unauthorized("missing or invalid API key")
