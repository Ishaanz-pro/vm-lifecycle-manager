"""Shared fixtures.

Tests run the app with the background worker and reconcile loop DISABLED and drive them
explicitly (`h.drain()`, `h.reconciler.run_once()`), with an injectable clock. That keeps every
test deterministic: no sleeps, no polling, no timing-dependent assertions.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from vmlm.config import HostSpec, Settings
from vmlm.domain.failures import FailureInjector
from vmlm.domain.reconciler import Reconciler
from vmlm.domain.service import LifecycleService
from vmlm.domain.worker import Worker
from vmlm.main import create_app
from vmlm.providers.simulated import SimulatedProvider

BODY = {"name": "web-1", "vcpus": 2, "memory_mib": 2048}
DEFAULT_HOSTS = [HostSpec(name="host-1", vcpus=8, memory_mib=16384)]


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Harness:
    app: FastAPI
    client: TestClient
    provider: SimulatedProvider
    clock: FakeClock
    settings: Settings
    headers: dict[str, str]

    @property
    def worker(self) -> Worker:
        return self.app.state.worker  # type: ignore[no-any-return]

    @property
    def service(self) -> LifecycleService:
        return self.app.state.service  # type: ignore[no-any-return]

    @property
    def reconciler(self) -> Reconciler:
        return self.app.state.reconciler  # type: ignore[no-any-return]

    @property
    def injector(self) -> FailureInjector:
        return self.app.state.injector  # type: ignore[no-any-return]

    # ---- HTTP helpers ------------------------------------------------------------------------

    def get(self, path: str, **kw: Any) -> Response:
        kw.setdefault("headers", {})
        return self.client.get(path, **{**kw, "headers": {**self.headers, **kw["headers"]}})

    def post(self, path: str, **kw: Any) -> Response:
        kw.setdefault("headers", {})
        return self.client.post(path, **{**kw, "headers": {**self.headers, **kw["headers"]}})

    def delete(self, path: str, **kw: Any) -> Response:
        kw.setdefault("headers", {})
        return self.client.delete(path, **{**kw, "headers": {**self.headers, **kw["headers"]}})

    def drain(self) -> int:
        return self.worker.run_until_idle()

    def submit(self, resp: Response, expect: int = 202) -> dict[str, Any]:
        assert resp.status_code == expect, resp.text
        op = resp.json()
        assert resp.headers["Location"] == f"/v1/operations/{op['id']}"
        return op  # type: ignore[no-any-return]

    def create(self, drain: bool = True, **overrides: Any) -> dict[str, Any]:
        op = self.submit(self.post("/v1/instances", json={**BODY, **overrides}))
        if drain:
            self.drain()
        return self.instance(op["instance_id"])

    def act(self, instance_id: str, action: str, drain: bool = True) -> dict[str, Any]:
        resp = (
            self.delete(f"/v1/instances/{instance_id}")
            if action == "terminate"
            else self.post(f"/v1/instances/{instance_id}/{action}")
        )
        op = self.submit(resp)
        if drain:
            self.drain()
        return self.operation(op["id"])

    def instance(self, instance_id: str) -> dict[str, Any]:
        resp = self.get(f"/v1/instances/{instance_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()  # type: ignore[no-any-return]

    def operation(self, operation_id: str) -> dict[str, Any]:
        resp = self.get(f"/v1/operations/{operation_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()  # type: ignore[no-any-return]

    def capacity(self) -> dict[str, Any]:
        return self.get("/v1/hosts/capacity").json()  # type: ignore[no-any-return]

    def events(self, instance_id: str) -> list[dict[str, Any]]:
        return self.get(f"/v1/instances/{instance_id}/events", params={"limit": 100}).json()[
            "items"
        ]  # type: ignore[no-any-return]


def assert_error(resp: Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    return err  # type: ignore[no-any-return]


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def provider() -> SimulatedProvider:
    return SimulatedProvider()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


MakeHarness = Callable[..., Harness]


@pytest.fixture
def make_harness(
    db_url: str, provider: SimulatedProvider, clock: FakeClock
) -> Iterator[MakeHarness]:
    """Build an app + client. Call again with the same db to simulate a restart."""
    opened: list[TestClient] = []

    def _make(
        hosts: list[HostSpec] | None = None,
        prov: SimulatedProvider | None = None,
        start: bool = True,
        raise_server_exceptions: bool = True,
        **overrides: Any,
    ) -> Harness:
        defaults: dict[str, Any] = {
            "worker_enabled": False,
            "reconcile_interval_s": 0,
            "retry_base_delay_s": 0,
            "retry_max_delay_s": 0,
            "failure_injection_enabled": True,
        }
        settings = Settings(
            database_url=db_url, hosts=hosts or DEFAULT_HOSTS, **{**defaults, **overrides}
        )
        app = create_app(settings, prov or provider, clock=clock)
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        if start:
            client.__enter__()
            opened.append(client)
        headers = {}
        key = settings.admin_api_key or settings.api_key
        if key:
            headers["X-API-Key"] = key.get_secret_value()
        return Harness(app, client, prov or provider, clock, settings, headers)

    yield _make
    for client in reversed(opened):
        client.__exit__(None, None, None)


@pytest.fixture
def h(make_harness: MakeHarness) -> Harness:
    return make_harness()
