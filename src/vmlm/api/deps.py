"""Dependency-injection helpers. Services live on app.state, set by the app factory.

Two keys: VMLM_API_KEY protects /v1/*; VMLM_ADMIN_API_KEY additionally unlocks
/v1/admin/* and /v1/debug/*. When a key is unset, that tier is open (local development).
Comparison is constant-time. Keys are never logged or echoed.
"""

import secrets
from typing import Annotated

from fastapi import Header, Request
from pydantic import SecretStr

from vmlm.config import Settings
from vmlm.domain.errors import FeatureDisabled, Forbidden, Unauthorized
from vmlm.domain.failures import FailureInjector
from vmlm.domain.reconciler import Reconciler
from vmlm.domain.service import LifecycleService
from vmlm.domain.worker import Worker
from vmlm.providers.base import VMProvider

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_service(request: Request) -> LifecycleService:
    return request.app.state.service  # type: ignore[no-any-return]


def get_worker(request: Request) -> Worker:
    return request.app.state.worker  # type: ignore[no-any-return]


def get_reconciler(request: Request) -> Reconciler:
    return request.app.state.reconciler  # type: ignore[no-any-return]


def get_provider(request: Request) -> VMProvider:
    return request.app.state.provider  # type: ignore[no-any-return]


def get_injector(request: Request) -> FailureInjector:
    return request.app.state.injector  # type: ignore[no-any-return]


def _matches(presented: str | None, expected: SecretStr | None) -> bool:
    if presented is None or expected is None:
        return False
    # compare_digest on bytes: constant-time, and safe for non-ASCII input
    return secrets.compare_digest(presented.encode(), expected.get_secret_value().encode())


def require_api_key(request: Request, x_api_key: ApiKeyHeader = None) -> None:
    settings = get_settings(request)
    if settings.api_key is None:
        return
    if _matches(x_api_key, settings.api_key) or _matches(x_api_key, settings.admin_api_key):
        return
    raise Unauthorized("missing or invalid API key")


def require_admin_key(request: Request, x_api_key: ApiKeyHeader = None) -> None:
    settings = get_settings(request)
    if settings.admin_api_key is None:
        return
    if _matches(x_api_key, settings.admin_api_key):
        return
    if _matches(x_api_key, settings.api_key):
        raise Forbidden("admin API key required")
    raise Unauthorized("missing or invalid API key")


def require_failure_injection(request: Request) -> None:
    if not get_settings(request).failure_injection_enabled:
        raise FeatureDisabled(
            "failure injection is disabled; set VMLM_FAILURE_INJECTION_ENABLED=true (dev only)"
        )
