"""Crash windows (provider succeeded, DB update did not) and restart persistence."""

import logging
from collections.abc import Callable
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import ServiceKit
from vmlm.db.models import AuditEvent
from vmlm.domain.states import VMState
from vmlm.providers.base import ProviderVMState
from vmlm.providers.simulated import SimulatedProvider


class Crash(RuntimeError):
    """Stands in for a process crash / DB outage between the provider call and the DB commit."""


def _boom(*_a: object, **_k: object) -> NoReturn:
    raise Crash("simulated crash before DB update")


def test_provision_succeeded_externally_but_db_update_lost(
    make_service: Callable[..., ServiceKit],
    provider: SimulatedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crashing = make_service()
    monkeypatch.setattr(crashing.service, "_complete", _boom)
    with pytest.raises(Crash):
        crashing.service.create_instance("vm", 2, 512)

    survivor = make_service()  # "restarted" control plane
    [stuck] = survivor.service.list_instances(None, 10, 0)[0]
    assert stuck.state is VMState.PROVISIONING  # visible marker, not a silent inconsistency
    assert provider.snapshot() == {stuck.provider_id: ProviderVMState.RUNNING}

    assert survivor.service.reconcile() == 1
    fixed = survivor.service.get_instance(stuck.id)
    assert fixed.state is VMState.RUNNING
    cap = survivor.service.capacity()
    assert (cap.reserved_vcpus, cap.active_reservations) == (2, 1)  # still reserved, once
    assert survivor.service.reconcile() == 0  # idempotent


def test_crash_before_provider_ran_is_failed_and_released(
    make_service: Callable[..., ServiceKit],
    provider: SimulatedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_service()
    monkeypatch.setattr(provider, "provision", _boom)
    with pytest.raises(Crash):
        kit.service.create_instance("vm", 2, 512)
    assert kit.service.capacity().reserved_vcpus == 2  # leaked until reconciled

    assert kit.service.reconcile() == 1
    [inst] = kit.service.list_instances(None, 10, 0)[0]
    assert inst.state is VMState.FAILED
    assert kit.service.capacity().reserved_vcpus == 0
    assert provider.snapshot() == {}


def test_crash_after_stop_and_after_terminate_are_resolved(
    make_service: Callable[..., ServiceKit],
    provider: SimulatedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_service()
    stopping, _ = kit.service.create_instance("a", 1, 100)
    terminating, _ = kit.service.create_instance("b", 1, 100)
    with monkeypatch.context() as m:
        m.setattr(kit.service, "_complete", _boom)
        with pytest.raises(Crash):
            kit.service.stop_instance(stopping.id)
        with pytest.raises(Crash):
            kit.service.terminate_instance(terminating.id)
    assert kit.service.get_instance(stopping.id).state is VMState.STOPPING
    assert kit.service.get_instance(terminating.id).state is VMState.TERMINATING

    assert kit.service.reconcile() == 2
    assert kit.service.get_instance(stopping.id).state is VMState.STOPPED
    assert kit.service.get_instance(terminating.id).state is VMState.TERMINATED
    cap = kit.service.capacity()
    assert (cap.reserved_vcpus, cap.active_reservations) == (1, 1)  # only "a" still holds


def test_reconcile_skips_when_provider_unreachable(
    make_service: Callable[..., ServiceKit],
    provider: SimulatedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_service()
    monkeypatch.setattr(provider, "provision", _boom)
    with pytest.raises(Crash):
        kit.service.create_instance("vm", 1, 100)
    monkeypatch.undo()
    provider.fail_next("inspect")
    assert kit.service.reconcile() == 0  # skipped, not guessed
    assert kit.service.list_instances(None, 10, 0)[0][0].state is VMState.PROVISIONING
    assert kit.service.reconcile() == 1


def test_reconcile_is_recorded_in_audit_log(
    make_service: Callable[..., ServiceKit], monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = make_service()
    with monkeypatch.context() as m:
        m.setattr(kit.service, "_complete", _boom)
        with pytest.raises(Crash):
            kit.service.create_instance("vm", 1, 100)
    kit.service.reconcile()
    with kit.session_factory() as s:
        details = [
            e.detail for e in s.scalars(select(AuditEvent)) if e.event_type == "state_changed"
        ]
    assert any(d and d.startswith("reconciled from PROVISIONING") for d in details)


def test_startup_runs_reconcile(
    make_app: Callable[..., FastAPI],
    make_service: Callable[..., ServiceKit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_service()
    with monkeypatch.context() as m:
        m.setattr(kit.service, "_complete", _boom)
        with pytest.raises(Crash):
            kit.service.create_instance("vm", 1, 100)
    with TestClient(make_app()) as c:  # lifespan startup reconciles
        items = c.get("/v1/instances").json()["items"]
        assert items[0]["state"] == "RUNNING"


# ---- restart persistence ----------------------------------------------------------------


def test_state_and_reservations_survive_restart(
    make_app: Callable[..., FastAPI], provider: SimulatedProvider
) -> None:
    body = {"name": "a", "vcpus": 2, "memory_mib": 1000}
    with TestClient(make_app()) as c1:
        a = c1.post("/v1/instances", json=body).json()
        b = c1.post("/v1/instances", json={**body, "name": "b"}).json()
        c1.post(f"/v1/instances/{b['id']}/stop")
        gone = c1.post("/v1/instances", json={**body, "name": "c"}).json()
        c1.delete(f"/v1/instances/{gone['id']}")
        cap_before = c1.get("/v1/hosts/capacity").json()
        events_before = c1.get(f"/v1/instances/{b['id']}/events").json()
    # The provider object plays the external hypervisor, which outlives the control plane.
    with TestClient(make_app(prov=provider)) as c2:
        assert c2.get(f"/v1/instances/{a['id']}").json()["state"] == "RUNNING"
        assert c2.get(f"/v1/instances/{b['id']}").json()["state"] == "STOPPED"
        assert c2.get(f"/v1/instances/{gone['id']}").json()["state"] == "TERMINATED"
        assert c2.get("/v1/hosts/capacity").json() == cap_before
        assert cap_before["reserved_vcpus"] == 4
        assert c2.get(f"/v1/instances/{b['id']}/events").json() == events_before

        # Reservations still enforce capacity: 4 of 8 vCPUs are held.
        resp = c2.post("/v1/instances", json={**body, "vcpus": 5})
        assert resp.status_code == 409
        # And operations keep working on pre-restart instances.
        assert c2.post(f"/v1/instances/{b['id']}/start").json()["state"] == "RUNNING"


def test_idempotency_keys_survive_restart(
    make_app: Callable[..., FastAPI], provider: SimulatedProvider
) -> None:
    body = {"name": "a", "vcpus": 1, "memory_mib": 100}
    headers = {"Idempotency-Key": "persist"}
    with TestClient(make_app()) as c1:
        first = c1.post("/v1/instances", json=body, headers=headers).json()
    with TestClient(make_app(prov=provider)) as c2:
        replay = c2.post("/v1/instances", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == first


# ---- logging ----------------------------------------------------------------------------


def test_state_changes_are_logged_with_structured_fields(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="vmlm")
    inst = client.post("/v1/instances", json={"name": "a", "vcpus": 1, "memory_mib": 1}).json()
    records = [r for r in caplog.records if r.getMessage() == "state_changed"]
    assert records and records[-1].instance_id == inst["id"]  # type: ignore[attr-defined]
    assert records[-1].to_state == "RUNNING"  # type: ignore[attr-defined]
