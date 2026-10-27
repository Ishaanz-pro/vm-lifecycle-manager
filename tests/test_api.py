"""API contract tests against the simulated provider, with the worker driven explicitly."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BODY, Harness, MakeHarness, assert_error
from vmlm.config import HostSpec
from vmlm.providers.base import ProviderVMState

INSTANCE_FIELDS = {
    "id", "name", "vcpus", "memory_mib", "state", "desired_state", "host_id", "provider_id",
    "failure_reason", "version", "created_at", "updated_at",
}  # fmt: skip
OPERATION_FIELDS = {
    "id", "instance_id", "action", "status", "from_state", "target_state", "attempts",
    "max_attempts", "error", "error_kind", "request_id", "created_at", "started_at",
    "completed_at", "next_attempt_at",
}  # fmt: skip


# ---- asynchronous contract ----------


def test_create_is_accepted_then_completed_by_worker(h: Harness) -> None:
    resp = h.post("/v1/instances", json=BODY)
    op = h.submit(resp, 202)
    assert set(op) == OPERATION_FIELDS
    assert (op["action"], op["status"], op["attempts"]) == ("create", "PENDING", 0)
    assert (op["from_state"], op["target_state"]) == ("PROVISIONING", "RUNNING")
    assert op["request_id"] == resp.headers["X-Request-ID"]

    inst = h.instance(op["instance_id"])
    assert set(inst) == INSTANCE_FIELDS
    assert inst["state"] == "PROVISIONING" and inst["desired_state"] == "RUNNING"
    assert h.capacity()["reserved_vcpus"] == 2  # reserved at accept time, not at completion
    assert h.provider.snapshot() == {}  # provider untouched until the worker runs

    assert h.drain() == 1
    done = h.operation(op["id"])
    assert done["status"] == "SUCCEEDED" and done["attempts"] == 1
    assert done["started_at"] and done["completed_at"] and done["error"] is None
    inst = h.instance(op["instance_id"])
    assert inst["state"] == "RUNNING" and inst["version"] == 2
    assert inst["host_id"] == h.get("/v1/hosts").json()["items"][0]["host_id"]
    assert h.provider.snapshot() == {inst["provider_id"]: ProviderVMState.RUNNING}


def test_wait_parameter_returns_200_when_finished(make_harness: MakeHarness) -> None:
    h = make_harness(worker_enabled=True)  # real background worker for this one
    resp = h.post("/v1/instances", json=BODY, params={"wait": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUCCEEDED"


def test_wait_parameter_returns_202_when_still_running(h: Harness) -> None:
    resp = h.post("/v1/instances", json=BODY, params={"wait": 0.05})
    assert resp.status_code == 202 and resp.json()["status"] == "PENDING"
    assert_error(h.post("/v1/instances", json=BODY, params={"wait": 31}), 422, "validation_error")


def test_request_id_is_echoed_or_generated(h: Harness) -> None:
    resp = h.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert resp.headers["X-Request-ID"] == "trace-abc"
    generated = h.get("/health").headers["X-Request-ID"]
    assert len(generated) == 16


# ---- validation / capacity ----------


@pytest.mark.parametrize(
    "override",
    [
        {"vcpus": 0}, {"vcpus": -1}, {"memory_mib": 0}, {"memory_mib": -512},
        {"vcpus": "2"}, {"vcpus": 1.5}, {"vcpus": True}, {"name": ""}, {"name": "-bad"},
        {"name": "has space"}, {"name": "x" * 65}, {"extra": 1}, {"vcpus": 1025},
    ],
)  # fmt: skip
def test_invalid_requests_rejected_without_side_effects(
    h: Harness, override: dict[str, Any]
) -> None:
    err = assert_error(h.post("/v1/instances", json={**BODY, **override}), 422, "validation_error")
    assert err["details"]["errors"]
    assert h.get("/v1/instances").json()["total"] == 0
    assert h.capacity()["reserved_vcpus"] == 0


def test_capacity_exceeded_rejected_without_side_effects(h: Harness) -> None:
    h.create(vcpus=6, memory_mib=1024)
    before = h.capacity()
    err = assert_error(
        h.post("/v1/instances", json={**BODY, "vcpus": 3}), 409, "insufficient_capacity"
    )
    assert (
        err["details"]["available_vcpus"] == 2
        and err["details"]["reason"] == "insufficient_capacity"
    )
    assert_error(
        h.post("/v1/instances", json={**BODY, "memory_mib": 16000}), 409, "insufficient_capacity"
    )
    assert h.capacity() == before
    assert h.get("/v1/instances").json()["total"] == 1


def test_placement_spreads_over_hosts_by_best_fit(make_harness: MakeHarness) -> None:
    h = make_harness(
        hosts=[
            HostSpec(name="big", vcpus=16, memory_mib=32768),
            HostSpec(name="small", vcpus=4, memory_mib=4096),
        ]
    )
    hosts = {x["name"]: x for x in h.get("/v1/hosts").json()["items"]}
    a = h.create(name="a", vcpus=2, memory_mib=4096)
    assert a["host_id"] == hosts["small"]["host_id"]
    b = h.create(name="b", vcpus=2, memory_mib=4096)
    assert b["host_id"] == hosts["big"]["host_id"]
    cap = h.capacity()
    assert cap["reserved_vcpus"] == 4 and cap["active_reservations"] == 2
    assert {x["name"]: x["reserved_memory_mib"] for x in cap["hosts"]} == {
        "big": 4096,
        "small": 4096,
    }


# ---- lifecycle ----------


def test_stop_then_start(h: Harness) -> None:
    inst = h.create()
    op = h.act(inst["id"], "stop")
    assert op["status"] == "SUCCEEDED" and (op["from_state"], op["target_state"]) == (
        "STOPPING",
        "STOPPED",
    )
    stopped = h.instance(inst["id"])
    assert stopped["state"] == "STOPPED" and stopped["desired_state"] == "STOPPED"
    assert h.provider.snapshot()[inst["provider_id"]] is ProviderVMState.STOPPED
    assert h.capacity()["reserved_vcpus"] == 2  # a stopped VM still holds its reservation

    h.act(inst["id"], "start")
    started = h.instance(inst["id"])
    assert started["state"] == "RUNNING" and started["desired_state"] == "RUNNING"
    assert started["version"] == inst["version"] + 4
    assert h.provider.snapshot()[inst["provider_id"]] is ProviderVMState.RUNNING


def test_illegal_transitions_rejected(h: Harness) -> None:
    inst = h.create()
    path = f"/v1/instances/{inst['id']}"
    err = assert_error(h.post(f"{path}/start"), 409, "invalid_transition")
    assert err["details"] == {"current_state": "RUNNING", "target_state": "STARTING"}

    h.act(inst["id"], "stop")
    assert_error(h.post(f"{path}/stop"), 409, "invalid_transition")

    h.act(inst["id"], "terminate")
    for call in (h.post(f"{path}/start"), h.post(f"{path}/stop"), h.delete(path)):
        assert_error(call, 409, "invalid_transition")
    assert h.instance(inst["id"])["state"] == "TERMINATED"


def test_duplicate_request_while_operation_in_flight_is_rejected(h: Harness) -> None:
    inst = h.create()
    first = h.submit(h.post(f"/v1/instances/{inst['id']}/stop"))
    err = assert_error(h.post(f"/v1/instances/{inst['id']}/stop"), 409, "invalid_transition")
    assert err["details"]["current_state"] == "STOPPING"
    assert_error(h.delete(f"/v1/instances/{inst['id']}"), 409, "invalid_transition")
    h.drain()
    assert h.operation(first["id"])["status"] == "SUCCEEDED"
    assert [c for c in h.provider.calls if c[0] == "stop"] == [("stop", inst["provider_id"])]


def test_terminate_releases_resources_exactly_once(h: Harness) -> None:
    keep = h.create(name="keep", vcpus=1, memory_mib=512)
    gone = h.create(name="gone")
    assert h.capacity()["reserved_vcpus"] == 3

    op = h.act(gone["id"], "terminate")
    assert op["status"] == "SUCCEEDED"
    assert h.instance(gone["id"])["state"] == "TERMINATED"
    cap = h.capacity()
    assert (cap["reserved_vcpus"], cap["reserved_memory_mib"], cap["active_reservations"]) == (
        1,
        512,
        1,
    )
    assert gone["provider_id"] not in h.provider.snapshot()
    assert keep["provider_id"] in h.provider.snapshot()

    assert_error(h.delete(f"/v1/instances/{gone['id']}"), 409, "invalid_transition")
    assert h.capacity() == cap
    released = [e for e in h.events(gone["id"]) if e["event_type"] == "resources_released"]
    assert len(released) == 1
    assert h.get(f"/v1/instances/{gone['id']}").status_code == 200  # record kept for audit


def test_terminate_from_stopped_and_from_failed(h: Harness) -> None:
    a = h.create(name="a")
    h.act(a["id"], "stop")
    assert h.act(a["id"], "terminate")["status"] == "SUCCEEDED"

    h.provider.fail_next("provision")
    b = h.create(name="b")
    assert b["state"] == "FAILED"
    assert h.act(b["id"], "terminate")["status"] == "SUCCEEDED"
    assert h.capacity()["reserved_vcpus"] == 0


# ---- provider failures (permanent) ----------


def test_provisioning_failure_rolls_back_resources(h: Harness) -> None:
    before = h.capacity()
    h.provider.fail_next("provision")
    op = h.submit(h.post("/v1/instances", json=BODY))
    h.drain()
    op = h.operation(op["id"])
    assert (op["status"], op["error_kind"], op["attempts"]) == ("FAILED", "permanent", 1)
    assert "simulated provision failure" in op["error"]

    assert h.capacity() == before
    assert h.provider.snapshot() == {}
    inst = h.instance(op["instance_id"])
    assert inst["state"] == "FAILED" and "provision failure" in inst["failure_reason"]
    kinds = [(e["event_type"], e["to_state"]) for e in h.events(inst["id"])]
    assert kinds == [
        ("created", "PROVISIONING"),
        ("resources_reserved", None),
        ("operation_queued", None),
        ("state_changed", "FAILED"),
        ("resources_released", None),
    ]
    h.create(vcpus=8, memory_mib=16384)  # capacity really is reusable


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_stop_failure_marks_failed_and_keeps_reservation(h: Harness, action: str) -> None:
    inst = h.create()
    if action == "start":
        h.act(inst["id"], "stop")
    h.provider.fail_next(action)
    op = h.act(inst["id"], action)
    assert op["status"] == "FAILED" and op["error_kind"] == "permanent"
    after = h.instance(inst["id"])
    assert after["state"] == "FAILED" and f"{action} failed" in after["failure_reason"]
    assert h.capacity()["reserved_vcpus"] == 2  # the VM may still exist: keep the reservation
    assert_error(h.post(f"/v1/instances/{inst['id']}/start"), 409, "invalid_transition")
    assert h.act(inst["id"], "terminate")["status"] == "SUCCEEDED"
    assert h.capacity()["reserved_vcpus"] == 0


def test_terminate_failure_keeps_reservation_and_can_be_retried(h: Harness) -> None:
    inst = h.create()
    h.provider.fail_next("delete")
    assert h.act(inst["id"], "terminate")["status"] == "FAILED"
    assert h.instance(inst["id"])["state"] == "FAILED"
    assert h.capacity()["reserved_vcpus"] == 2
    assert h.act(inst["id"], "terminate")["status"] == "SUCCEEDED"
    assert h.capacity()["reserved_vcpus"] == 0


# ---- audit / operations / lookups ----------


def test_audit_events_record_full_lifecycle_with_operation_ids(h: Harness) -> None:
    inst = h.create()
    ops = []
    for a in ("stop", "start", "terminate"):
        h.clock.advance(1)  # operations list by created_at, then id
        ops.append(h.act(inst["id"], a)["id"])
    evs = h.events(inst["id"])
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
    assert all(e["request_id"] for e in evs)

    page = h.get(f"/v1/instances/{inst['id']}/operations").json()
    assert page["total"] == 4
    assert [o["action"] for o in page["items"]] == ["create", "stop", "start", "terminate"]
    assert all(o["status"] == "SUCCEEDED" for o in page["items"])
    create_op = page["items"][0]["id"]
    changed = [e["operation_id"] for e in evs if e["event_type"] == "state_changed"]
    assert changed == [create_op, ops[0], ops[0], ops[1], ops[1], ops[2], ops[2]]


def test_events_are_paginated(h: Harness) -> None:
    inst = h.create()
    page = h.get(f"/v1/instances/{inst['id']}/events", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 4 and page["limit"] == 2 and page["offset"] == 1
    assert [e["event_type"] for e in page["items"]] == ["resources_reserved", "operation_queued"]


def test_missing_instance_and_operation_return_404(h: Harness) -> None:
    path = "/v1/instances/does-not-exist"
    for resp in (
        h.get(path), h.post(f"{path}/start"), h.post(f"{path}/stop"), h.delete(path),
        h.get(f"{path}/events"), h.get(f"{path}/operations"),
    ):  # fmt: skip
        err = assert_error(resp, 404, "instance_not_found")
        assert err["details"] == {"instance_id": "does-not-exist"}
    assert_error(h.get("/v1/operations/nope"), 404, "operation_not_found")
    assert_error(h.get("/v1/nope"), 404, "http_error")


def test_list_pagination_and_state_filter(h: Harness) -> None:
    ids = []
    for i in range(3):  # listing is ordered by created_at then id; the fake clock must move
        ids.append(h.create(name=f"vm-{i}", vcpus=1, memory_mib=64)["id"])
        h.clock.advance(1)
    h.act(ids[1], "stop")
    page = h.get("/v1/instances", params={"limit": 2}).json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    assert [i["id"] for i in page["items"]] == ids[:2]
    assert [
        i["id"] for i in h.get("/v1/instances", params={"limit": 2, "offset": 2}).json()["items"]
    ] == ids[2:]
    stopped = h.get("/v1/instances", params={"state": "STOPPED"}).json()
    assert stopped["total"] == 1 and stopped["items"][0]["id"] == ids[1]
    for bad in ({"limit": 0}, {"limit": 101}, {"state": "BOGUS"}):
        assert_error(h.get("/v1/instances", params=bad), 422, "validation_error")


# ---- idempotency ----------


def test_same_key_replays_create_without_reserving_twice(h: Harness) -> None:
    headers = {"Idempotency-Key": "abc-123"}
    first = h.post("/v1/instances", json=BODY, headers=headers)
    second = h.post("/v1/instances", json=BODY, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json() == first.json()
    assert h.get("/v1/instances").json()["total"] == 1
    assert h.capacity()["reserved_vcpus"] == 2
    h.drain()
    third = h.post("/v1/instances", json=BODY, headers=headers)  # replay after completion
    assert third.status_code == 200 and third.json()["status"] == "SUCCEEDED"


def test_same_key_different_body_is_a_conflict(h: Harness) -> None:
    headers = {"Idempotency-Key": "k"}
    h.post("/v1/instances", json=BODY, headers=headers)
    assert_error(
        h.post("/v1/instances", json={**BODY, "vcpus": 4}, headers=headers),
        409,
        "idempotency_conflict",
    )
    assert h.get("/v1/instances").json()["total"] == 1


def test_lifecycle_actions_are_idempotent_per_action_and_instance(h: Harness) -> None:
    a, b = h.create(name="a"), h.create(name="b")
    headers = {"Idempotency-Key": "stop-once"}
    first = h.post(f"/v1/instances/{a['id']}/stop", headers=headers)
    again = h.post(f"/v1/instances/{a['id']}/stop", headers=headers)
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["id"] == first.json()["id"]
    # the same key on another instance / action is a different scope, not a replay
    assert h.post(f"/v1/instances/{b['id']}/stop", headers=headers).status_code == 202
    h.drain()
    assert h.post(f"/v1/instances/{a['id']}/start", headers=headers).status_code == 202
    h.drain()
    assert [c for c in h.provider.calls if c[0] == "stop"] == [
        ("stop", a["provider_id"]),
        ("stop", b["provider_id"]),
    ]


def test_idempotency_keys_expire(make_harness: MakeHarness) -> None:
    h = make_harness(idempotency_ttl_s=60)
    headers = {"Idempotency-Key": "ttl"}
    h.post("/v1/instances", json=BODY, headers=headers)
    h.clock.advance(59)
    assert h.post("/v1/instances", json=BODY, headers=headers).status_code == 200
    h.clock.advance(2)
    assert h.post("/v1/instances", json=BODY, headers=headers).status_code == 202  # new instance
    assert h.get("/v1/instances").json()["total"] == 2


def test_no_key_means_no_deduplication(h: Harness) -> None:
    h.create()
    h.create()
    assert h.get("/v1/instances").json()["total"] == 2


def test_rejected_create_does_not_consume_the_key(h: Harness) -> None:
    full = h.create(vcpus=8)
    headers = {"Idempotency-Key": "retry-me"}
    assert_error(
        h.post("/v1/instances", json={**BODY, "vcpus": 8}, headers=headers),
        409,
        "insufficient_capacity",
    )
    h.act(full["id"], "terminate")
    assert h.post("/v1/instances", json={**BODY, "vcpus": 8}, headers=headers).status_code == 202


# ---- hosts / admin ----------


def test_hosts_endpoint_and_capacity_summary(h: Harness) -> None:
    page = h.get("/v1/hosts").json()
    assert page["total"] == 1
    host = page["items"][0]
    assert set(host) == {
        "host_id", "name", "status", "total_vcpus", "reserved_vcpus", "available_vcpus",
        "total_memory_mib", "reserved_memory_mib", "available_memory_mib", "active_reservations",
    }  # fmt: skip
    assert (host["name"], host["status"], host["total_vcpus"]) == ("host-1", "READY", 8)
    cap = h.capacity()
    assert cap["hosts"] == page["items"]


def test_draining_host_rejects_new_placements_but_keeps_instances(h: Harness) -> None:
    inst = h.create()
    resp = h.post("/v1/admin/hosts/host-1/status", json={"status": "DRAINING"})
    assert resp.status_code == 200 and resp.json()["status"] == "DRAINING"
    err = assert_error(h.post("/v1/instances", json=BODY), 409, "insufficient_capacity")
    assert err["details"]["reason"] == "no_eligible_hosts"
    assert h.act(inst["id"], "stop")["status"] == "SUCCEEDED"  # existing instances still operate
    assert_error(
        h.post("/v1/admin/hosts/nope/status", json={"status": "READY"}), 404, "host_not_found"
    )
    h.post("/v1/admin/hosts/host-1/status", json={"status": "READY"})
    assert h.post("/v1/instances", json=BODY).status_code == 202


def test_heartbeat_updates_capacity_and_health(make_harness: MakeHarness) -> None:
    h = make_harness(
        hosts=[HostSpec(name="agent-host", vcpus=2, memory_mib=1024, agent_managed=True)]
    )
    resp = h.post(
        "/v1/admin/hosts/agent-host/heartbeat",
        json={"healthy": True, "vcpus": 12, "memory_mib": 8192, "agent_version": "0.1.0"},
    )
    assert resp.status_code == 200
    assert (resp.json()["total_vcpus"], resp.json()["total_memory_mib"], resp.json()["status"]) == (
        12,
        8192,
        "READY",
    )
    assert (
        h.post("/v1/admin/hosts/agent-host/heartbeat", json={"healthy": False}).json()["status"]
        == "UNHEALTHY"
    )
    assert_error(h.post("/v1/instances", json=BODY), 409, "insufficient_capacity")
    assert_error(
        h.post("/v1/admin/hosts/agent-host/heartbeat", json={"healthy": True, "bogus": 1}),
        422,
        "validation_error",
    )


# ---- authentication ----------


def test_api_key_and_admin_key_tiers(make_harness: MakeHarness) -> None:
    h = make_harness(api_key="user-key", admin_api_key="admin-key")
    c: TestClient = h.client
    assert_error(c.get("/v1/instances"), 401, "unauthorized")
    assert_error(c.get("/v1/instances", headers={"X-API-Key": "wrong"}), 401, "unauthorized")
    assert c.get("/v1/instances", headers={"X-API-Key": "user-key"}).status_code == 200
    assert c.get("/v1/instances", headers={"X-API-Key": "admin-key"}).status_code == 200
    assert_error(c.post("/v1/admin/reconcile", headers={"X-API-Key": "user-key"}), 403, "forbidden")
    assert_error(c.post("/v1/admin/reconcile"), 401, "unauthorized")
    assert c.post("/v1/admin/reconcile", headers={"X-API-Key": "admin-key"}).status_code == 200
    assert c.get("/health").status_code == 200  # health/ready/metrics stay open
    assert c.get("/ready").status_code == 200
    assert c.get("/metrics").status_code == 200


def test_auth_disabled_by_default(h: Harness) -> None:
    assert h.client.get("/v1/instances").status_code == 200


# ---- debug / failure injection gating ----------


def test_failure_injection_endpoint_is_gated(make_harness: MakeHarness) -> None:
    h = make_harness(failure_injection_enabled=False)
    assert_error(
        h.post("/v1/debug/failures", json={"kind": "worker_crash"}), 403, "feature_disabled"
    )
    assert_error(h.get("/v1/debug/failures"), 403, "feature_disabled")


def test_failure_injection_endpoint_arms_failures(h: Harness) -> None:
    resp = h.post(
        "/v1/debug/failures",
        json={"kind": "provider_failure", "operation": "provision", "retryable": True, "times": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["provider_failures"] == {"provision": 2}
    assert (
        h.post("/v1/debug/failures", json={"kind": "worker_crash"}).json()["control_plane"][
            "worker_crash"
        ]
        == 1
    )
    assert h.post("/v1/debug/failures", json={"kind": "provider_failure"}).json()["error"]
    status = h.get("/v1/debug/failures").json()
    assert (
        status["provider_failures"] == {"provision": 2}
        and status["control_plane"]["worker_crash"] == 1
    )


# ---- health / readiness ----------


def test_health_and_ready(h: Harness) -> None:
    health = h.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["provider"] == {"name": "simulated", "simulated": True, "real_vms": False}
    ready = h.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"database": True, "hosts_seeded": True, "worker": True},
    }


def test_ready_reports_dead_worker(make_harness: MakeHarness) -> None:
    h = make_harness(worker_enabled=True)
    h.app.state.worker.thread.join(timeout=0)  # alive
    assert h.get("/ready").json()["checks"]["worker"] is True
    h.client.__exit__(None, None, None)  # shuts the worker down
    resp = h.client.get("/ready")
    assert resp.status_code == 503 and resp.json()["checks"]["worker"] is False
    h.client.__enter__()  # let the fixture close it cleanly
