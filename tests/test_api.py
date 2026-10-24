from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vmlm.providers.base import ProviderVMState
from vmlm.providers.simulated import SimulatedProvider

BODY = {"name": "web-1", "vcpus": 2, "memory_mib": 2048}


def create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    resp = client.post("/v1/instances", json={**BODY, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def capacity(client: TestClient) -> dict[str, int]:
    return client.get("/v1/hosts/capacity").json()  # type: ignore[no-any-return]


def events(client: TestClient, instance_id: str) -> list[dict[str, Any]]:
    return client.get(f"/v1/instances/{instance_id}/events").json()["items"]  # type: ignore[no-any-return]


def assert_error(resp: Any, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    return err  # type: ignore[no-any-return]


# ---- create / schemas / health --------------------------------------------------------


def test_create_valid_instance(client: TestClient, provider: SimulatedProvider) -> None:
    resp = client.post("/v1/instances", json=BODY)
    assert resp.status_code == 201
    data = resp.json()
    assert set(data) == {
        "id", "name", "vcpus", "memory_mib", "state", "provider_id",
        "failure_reason", "version", "created_at", "updated_at",
    }  # fmt: skip
    assert (data["name"], data["vcpus"], data["memory_mib"]) == ("web-1", 2, 2048)
    assert data["state"] == "RUNNING"
    assert data["failure_reason"] is None
    assert data["version"] == 2  # PROVISIONING (1) -> RUNNING (2)
    assert resp.headers["Location"] == f"/v1/instances/{data['id']}"
    assert provider.snapshot() == {data["provider_id"]: ProviderVMState.RUNNING}
    cap = capacity(client)
    assert (cap["reserved_vcpus"], cap["reserved_memory_mib"]) == (2, 2048)
    assert client.get(f"/v1/instances/{data['id']}").json() == data


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


@pytest.mark.parametrize(
    "override",
    [
        {"vcpus": 0}, {"vcpus": -1}, {"memory_mib": 0}, {"memory_mib": -512},
        {"vcpus": "2"}, {"vcpus": 1.5}, {"vcpus": True}, {"name": ""}, {"name": "-bad"},
        {"name": "has space"}, {"name": "x" * 65}, {"extra": 1},
    ],
)  # fmt: skip
def test_invalid_resource_requests_rejected(client: TestClient, override: dict[str, Any]) -> None:
    resp = client.post("/v1/instances", json={**BODY, **override})
    err = assert_error(resp, 422, "validation_error")
    assert err["details"]["errors"]
    assert client.get("/v1/instances").json()["total"] == 0
    assert capacity(client)["reserved_vcpus"] == 0


def test_missing_field_rejected(client: TestClient) -> None:
    resp = client.post("/v1/instances", json={"name": "a", "vcpus": 1})
    err = assert_error(resp, 422, "validation_error")
    assert err["details"]["errors"][0]["field"] == "body.memory_mib"


def test_capacity_exceeded_rejected_without_side_effects(client: TestClient) -> None:
    create(client, vcpus=6, memory_mib=1024)
    before = capacity(client)
    err = assert_error(
        client.post("/v1/instances", json={**BODY, "vcpus": 3}), 409, "insufficient_capacity"
    )
    assert err["details"]["available_vcpus"] == 2
    assert_error(
        client.post("/v1/instances", json={**BODY, "memory_mib": 16000}),
        409,
        "insufficient_capacity",
    )
    assert capacity(client) == before
    assert client.get("/v1/instances").json()["total"] == 1


def test_request_larger_than_whole_host_rejected(client: TestClient) -> None:
    assert_error(
        client.post("/v1/instances", json={**BODY, "vcpus": 1000}), 409, "insufficient_capacity"
    )


# ---- lifecycle --------------------------------------------------------------------------


def test_stop_then_start(client: TestClient, provider: SimulatedProvider) -> None:
    inst = create(client)
    stopped = client.post(f"/v1/instances/{inst['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "STOPPED"
    assert provider.snapshot()[inst["provider_id"]] is ProviderVMState.STOPPED
    assert capacity(client)["reserved_vcpus"] == 2  # a stopped VM still holds its reservation

    started = client.post(f"/v1/instances/{inst['id']}/start")
    assert started.json()["state"] == "RUNNING"
    assert started.json()["version"] == inst["version"] + 4
    assert provider.snapshot()[inst["provider_id"]] is ProviderVMState.RUNNING


def test_illegal_transitions_rejected(client: TestClient) -> None:
    inst = create(client)
    path = f"/v1/instances/{inst['id']}"
    err = assert_error(client.post(f"{path}/start"), 409, "invalid_transition")
    assert err["details"] == {"current_state": "RUNNING", "target_state": "STARTING"}

    client.post(f"{path}/stop")
    assert_error(client.post(f"{path}/stop"), 409, "invalid_transition")

    client.delete(path)
    for call in (client.post(f"{path}/start"), client.post(f"{path}/stop"), client.delete(path)):
        assert_error(call, 409, "invalid_transition")
    assert client.get(path).json()["state"] == "TERMINATED"


def test_delete_releases_resources_exactly_once(
    client: TestClient, provider: SimulatedProvider
) -> None:
    keep = create(client, name="keep", vcpus=1, memory_mib=512)
    gone = create(client, name="gone")
    assert capacity(client)["reserved_vcpus"] == 3

    resp = client.delete(f"/v1/instances/{gone['id']}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "TERMINATED"
    cap = capacity(client)
    assert (cap["reserved_vcpus"], cap["reserved_memory_mib"], cap["active_reservations"]) == (
        1, 512, 1,
    )  # fmt: skip
    assert gone["provider_id"] not in provider.snapshot()
    assert keep["provider_id"] in provider.snapshot()

    assert_error(client.delete(f"/v1/instances/{gone['id']}"), 409, "invalid_transition")
    assert capacity(client) == cap  # second delete released nothing more
    released = [e for e in events(client, gone["id"]) if e["event_type"] == "resources_released"]
    assert len(released) == 1
    # Terminated record is kept for audit and still readable.
    assert client.get(f"/v1/instances/{gone['id']}").status_code == 200


def test_terminate_from_stopped_state(client: TestClient) -> None:
    inst = create(client)
    client.post(f"/v1/instances/{inst['id']}/stop")
    assert client.delete(f"/v1/instances/{inst['id']}").json()["state"] == "TERMINATED"
    assert capacity(client)["reserved_vcpus"] == 0


# ---- failures ---------------------------------------------------------------------------


def test_provisioning_failure_rolls_back_resources(
    client: TestClient, provider: SimulatedProvider
) -> None:
    before = capacity(client)
    provider.fail_next("provision")
    resp = client.post("/v1/instances", json=BODY)
    err = assert_error(resp, 502, "provider_operation_failed")
    assert err["details"]["state"] == "FAILED"

    assert capacity(client) == before
    assert provider.snapshot() == {}
    inst = client.get(f"/v1/instances/{err['details']['instance_id']}").json()
    assert inst["state"] == "FAILED"
    assert "simulated provision failure" in inst["failure_reason"]
    kinds = [(e["event_type"], e["to_state"]) for e in events(client, inst["id"])]
    assert kinds == [
        ("created", "PROVISIONING"),
        ("resources_reserved", None),
        ("state_changed", "FAILED"),
        ("resources_released", None),
    ]

    # A failed instance can be cleaned up, and that must not release a second time.
    assert client.delete(f"/v1/instances/{inst['id']}").json()["state"] == "TERMINATED"
    assert capacity(client) == before
    released = [e for e in events(client, inst["id"]) if e["event_type"] == "resources_released"]
    assert len(released) == 1
    # Capacity is truly reusable afterwards.
    create(client, vcpus=8, memory_mib=16384)


@pytest.mark.parametrize("op,start_state", [("start", "STOPPED"), ("stop", "RUNNING")])
def test_start_stop_failure_marks_failed_and_keeps_reservation(
    client: TestClient, provider: SimulatedProvider, op: str, start_state: str
) -> None:
    inst = create(client)
    path = f"/v1/instances/{inst['id']}"
    if op == "start":
        client.post(f"{path}/stop")
    provider.fail_next(op)
    err = assert_error(client.post(f"{path}/{op}"), 502, "provider_operation_failed")
    assert err["details"] == {"instance_id": inst["id"], "state": "FAILED"}

    after = client.get(path).json()
    assert after["state"] == "FAILED"
    assert f"{op} failed" in after["failure_reason"]
    assert capacity(client)["reserved_vcpus"] == 2  # VM may still exist: keep the reservation
    assert_error(client.post(f"{path}/start"), 409, "invalid_transition")

    assert client.delete(path).json()["state"] == "TERMINATED"
    assert capacity(client)["reserved_vcpus"] == 0


def test_terminate_failure_keeps_reservation_and_can_be_retried(
    client: TestClient, provider: SimulatedProvider
) -> None:
    inst = create(client)
    provider.fail_next("delete")
    assert_error(client.delete(f"/v1/instances/{inst['id']}"), 502, "provider_operation_failed")
    assert capacity(client)["reserved_vcpus"] == 2
    retry = client.delete(f"/v1/instances/{inst['id']}")
    assert retry.json()["state"] == "TERMINATED"
    assert capacity(client)["reserved_vcpus"] == 0


# ---- audit / lookups --------------------------------------------------------------------


def test_audit_events_record_full_lifecycle(client: TestClient) -> None:
    inst = create(client)
    path = f"/v1/instances/{inst['id']}"
    client.post(f"{path}/stop")
    client.post(f"{path}/start")
    client.delete(path)

    evs = events(client, inst["id"])
    transitions = [
        (e["from_state"], e["to_state"]) for e in evs if e["event_type"] == "state_changed"
    ]
    assert transitions == [
        ("PROVISIONING", "RUNNING"),
        ("RUNNING", "STOPPING"),
        ("STOPPING", "STOPPED"),
        ("STOPPED", "STARTING"),
        ("STARTING", "RUNNING"),
        ("RUNNING", "TERMINATING"),
        ("TERMINATING", "TERMINATED"),
    ]
    assert evs[0]["event_type"] == "created"
    assert evs[-1]["event_type"] == "resources_released"
    assert [e["id"] for e in evs] == sorted(e["id"] for e in evs)
    assert {e["instance_id"] for e in evs} == {inst["id"]}


def test_events_are_paginated(client: TestClient) -> None:
    inst = create(client)
    page = client.get(f"/v1/instances/{inst['id']}/events", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 1
    assert [e["event_type"] for e in page["items"]] == ["resources_reserved", "state_changed"]


def test_missing_instance_returns_404_everywhere(client: TestClient) -> None:
    path = "/v1/instances/does-not-exist"
    for resp in (
        client.get(path),
        client.post(f"{path}/start"),
        client.post(f"{path}/stop"),
        client.delete(path),
        client.get(f"{path}/events"),
    ):
        err = assert_error(resp, 404, "instance_not_found")
        assert err["details"] == {"instance_id": "does-not-exist"}


def test_unknown_route_uses_error_format(client: TestClient) -> None:
    assert_error(client.get("/v1/nope"), 404, "http_error")


def test_list_pagination_and_state_filter(client: TestClient) -> None:
    ids = [create(client, name=f"vm-{i}", vcpus=1, memory_mib=64)["id"] for i in range(3)]
    client.post(f"/v1/instances/{ids[1]}/stop")

    page = client.get("/v1/instances", params={"limit": 2}).json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    assert [i["id"] for i in page["items"]] == ids[:2]
    rest = client.get("/v1/instances", params={"limit": 2, "offset": 2}).json()
    assert [i["id"] for i in rest["items"]] == ids[2:]

    stopped = client.get("/v1/instances", params={"state": "STOPPED"}).json()
    assert stopped["total"] == 1 and stopped["items"][0]["id"] == ids[1]

    assert_error(client.get("/v1/instances", params={"limit": 0}), 422, "validation_error")
    assert_error(client.get("/v1/instances", params={"limit": 101}), 422, "validation_error")
    assert_error(client.get("/v1/instances", params={"state": "BOGUS"}), 422, "validation_error")


# ---- idempotency ------------------------------------------------------------------------


def test_duplicate_create_with_same_key_returns_same_instance(client: TestClient) -> None:
    headers = {"Idempotency-Key": "abc-123"}
    first = client.post("/v1/instances", json=BODY, headers=headers)
    second = client.post("/v1/instances", json=BODY, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert client.get("/v1/instances").json()["total"] == 1
    assert capacity(client)["reserved_vcpus"] == 2  # reserved once, not twice


def test_same_key_different_body_is_a_conflict(client: TestClient) -> None:
    headers = {"Idempotency-Key": "k"}
    client.post("/v1/instances", json=BODY, headers=headers)
    resp = client.post("/v1/instances", json={**BODY, "vcpus": 4}, headers=headers)
    assert_error(resp, 409, "idempotency_conflict")
    assert client.get("/v1/instances").json()["total"] == 1


def test_no_key_means_no_deduplication(client: TestClient) -> None:
    create(client)
    create(client)
    assert client.get("/v1/instances").json()["total"] == 2


def test_rejected_create_does_not_consume_the_key(client: TestClient) -> None:
    big = {**BODY, "vcpus": 8}
    full = create(client, vcpus=8)
    headers = {"Idempotency-Key": "retry-me"}
    assert_error(
        client.post("/v1/instances", json=big, headers=headers), 409, "insufficient_capacity"
    )
    client.delete(f"/v1/instances/{full['id']}")
    assert client.post("/v1/instances", json=big, headers=headers).status_code == 201


# ---- authentication ---------------------------------------------------------------------


def test_api_key_enforced_when_configured(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(make_app(api_key="s3cret")) as c:
        assert_error(c.get("/v1/instances"), 401, "unauthorized")
        assert_error(c.get("/v1/instances", headers={"X-API-Key": "wrong"}), 401, "unauthorized")
        assert c.get("/v1/instances", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/health").status_code == 200  # health stays open


def test_auth_disabled_by_default(client: TestClient) -> None:
    assert client.get("/v1/instances").status_code == 200
