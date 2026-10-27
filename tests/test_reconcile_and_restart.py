"""Reconciliation (drift, heartbeats, interrupted work) and persistence across restart."""

from tests.conftest import BODY, Harness, MakeHarness, assert_error
from vmlm.config import HostSpec
from vmlm.providers.base import ProviderVMState

# ---- drift ----------


def test_running_instance_found_stopped_is_corrected_and_restarted(h: Harness) -> None:
    inst = h.create()
    h.provider.set_external_state(
        inst["provider_id"], ProviderVMState.STOPPED
    )  # e.g. guest shut itself down
    report = h.reconciler.run_once()
    assert (
        report.instances_inspected,
        report.drift_corrected,
        report.restore_operations_queued,
    ) == (1, 1, 1)

    corrected = h.instance(inst["id"])
    assert corrected["state"] == "STARTING" and corrected["desired_state"] == "RUNNING"
    drift = [e for e in h.events(inst["id"]) if e["event_type"] == "drift_detected"]
    assert len(drift) == 1 and (drift[0]["from_state"], drift[0]["to_state"]) == (
        "RUNNING",
        "STOPPED",
    )
    assert drift[0]["request_id"] == "reconciler"

    h.drain()
    assert h.instance(inst["id"])["state"] == "RUNNING"
    assert h.provider.snapshot()[inst["provider_id"]] is ProviderVMState.RUNNING
    assert h.reconciler.run_once().drift_corrected == 0  # converged


def test_stopped_instance_found_running_is_stopped_again(h: Harness) -> None:
    inst = h.create()
    h.act(inst["id"], "stop")
    h.provider.set_external_state(inst["provider_id"], ProviderVMState.RUNNING)
    report = h.reconciler.run_once()
    assert report.drift_corrected == 1 and report.restore_operations_queued == 1
    h.drain()
    assert h.instance(inst["id"])["state"] == "STOPPED"
    assert h.provider.snapshot()[inst["provider_id"]] is ProviderVMState.STOPPED


def test_vm_missing_at_provider_is_marked_failed_not_recreated(h: Harness) -> None:
    inst = h.create()
    h.provider.set_external_state(inst["provider_id"], None)
    report = h.reconciler.run_once()
    assert report.marked_failed == 1 and report.restore_operations_queued == 0
    failed = h.instance(inst["id"])
    assert failed["state"] == "FAILED" and failed["failure_reason"] == "vm missing at provider"
    assert h.capacity()["reserved_vcpus"] == 2  # kept until an operator terminates
    assert [c[0] for c in h.provider.calls if c[0] == "provision"] == ["provision"]  # no re-create
    assert h.act(inst["id"], "terminate")["status"] == "SUCCEEDED"
    assert h.capacity()["reserved_vcpus"] == 0


def test_instances_with_work_in_flight_are_left_to_the_worker(h: Harness) -> None:
    inst = h.create()
    h.submit(h.post(f"/v1/instances/{inst['id']}/stop"))  # STOPPING, op PENDING
    h.provider.set_external_state(inst["provider_id"], None)
    report = h.reconciler.run_once()
    assert report.instances_inspected == 0 and report.marked_failed == 0


def test_reconcile_skips_instances_when_provider_inspect_fails(h: Harness) -> None:
    inst = h.create()
    h.provider.set_external_state(inst["provider_id"], None)
    h.provider.fail_next("inspect")
    report = h.reconciler.run_once()
    assert report.skipped_provider_errors == 1 and report.marked_failed == 0
    assert h.instance(inst["id"])["state"] == "RUNNING"  # not guessed
    assert h.reconciler.run_once().marked_failed == 1


def test_admin_reconcile_endpoint_returns_report(h: Harness) -> None:
    inst = h.create()
    h.provider.set_external_state(inst["provider_id"], ProviderVMState.STOPPED)
    body = h.post("/v1/admin/reconcile").json()
    assert (
        body["drift_corrected"] == 1
        and body["restore_operations_queued"] == 1
        and body["errors"] == []
    )


# ---- heartbeats ----------


def test_stale_heartbeat_marks_agent_host_unhealthy_and_recovers(make_harness: MakeHarness) -> None:
    h = make_harness(
        hosts=[
            HostSpec(name="static", vcpus=4, memory_mib=4096),
            HostSpec(name="agent", vcpus=8, memory_mib=8192, agent_managed=True),
        ],
        heartbeat_stale_s=30,
    )
    # never heard from the agent -> marked unhealthy by the startup reconcile pass
    status = {x["name"]: x["status"] for x in h.get("/v1/hosts").json()["items"]}
    assert status == {"static": "READY", "agent": "UNHEALTHY"}
    inst = h.create(vcpus=4, memory_mib=4096)  # lands on "static", agent host excluded
    assert_error(h.post("/v1/instances", json={**BODY, "vcpus": 1}), 409, "insufficient_capacity")

    h.post("/v1/admin/hosts/agent/heartbeat", json={"healthy": True})
    assert h.post("/v1/instances", json={**BODY, "vcpus": 1}).status_code == 202
    h.clock.advance(31)
    assert h.reconciler.run_once().hosts_marked_unhealthy == 1
    h.clock.advance(1)
    h.post("/v1/admin/hosts/agent/heartbeat", json={"healthy": True})
    assert h.reconciler.run_once().hosts_marked_unhealthy == 0
    assert h.instance(inst["id"])["state"] == "RUNNING"


def test_draining_is_not_overridden_by_heartbeats(make_harness: MakeHarness) -> None:
    h = make_harness(hosts=[HostSpec(name="agent", vcpus=8, memory_mib=8192, agent_managed=True)])
    h.post("/v1/admin/hosts/agent/status", json={"status": "DRAINING"})
    resp = h.post("/v1/admin/hosts/agent/heartbeat", json={"healthy": True})
    assert resp.json()["status"] == "DRAINING"


# ---- restart persistence ----------


def test_everything_survives_restart(make_harness: MakeHarness, provider) -> None:  # type: ignore[no-untyped-def]
    h1 = make_harness()
    a = h1.create(name="a", vcpus=2, memory_mib=1000)
    b = h1.create(name="b", vcpus=2, memory_mib=1000)
    h1.act(b["id"], "stop")
    gone = h1.create(name="c", vcpus=2, memory_mib=1000)
    h1.act(gone["id"], "terminate")
    pending = h1.submit(
        h1.post("/v1/instances", json={**BODY, "name": "d", "vcpus": 1, "memory_mib": 500})
    )
    key_resp = h1.post(
        "/v1/instances",
        json={**BODY, "name": "e", "vcpus": 1, "memory_mib": 500},
        headers={"Idempotency-Key": "persist"},
    )
    cap_before = h1.capacity()
    events_before = h1.events(b["id"])
    h1.client.__exit__(None, None, None)

    # The provider object plays the external hypervisor, which outlives the control plane.
    h2 = make_harness(prov=provider)
    assert h2.instance(a["id"])["state"] == "RUNNING"
    assert h2.instance(b["id"])["state"] == "STOPPED"
    assert h2.instance(gone["id"])["state"] == "TERMINATED"
    assert h2.capacity() == cap_before and cap_before["reserved_vcpus"] == 6
    assert h2.events(b["id"]) == events_before
    assert h2.operation(pending["id"])["status"] == "PENDING"  # durable, still waiting
    h2.drain()
    assert h2.operation(pending["id"])["status"] == "SUCCEEDED"
    assert h2.instance(pending["instance_id"])["state"] == "RUNNING"

    replay = h2.post(
        "/v1/instances",
        json={**BODY, "name": "e", "vcpus": 1, "memory_mib": 500},
        headers={"Idempotency-Key": "persist"},
    )
    assert replay.status_code == 200 and replay.json()["id"] == key_resp.json()["id"]
    assert_error(
        h2.post("/v1/instances", json={**BODY, "vcpus": 3}), 409, "insufficient_capacity"
    )  # 6/8 vCPU held
    assert h2.act(b["id"], "start")["status"] == "SUCCEEDED"


def test_startup_requeues_operation_whose_worker_died(make_harness: MakeHarness, provider) -> None:  # type: ignore[no-untyped-def]
    h1 = make_harness(provider_timeout_s=1, worker_lease_s=5)
    h1.injector.arm("worker_crash")
    op = h1.submit(h1.post("/v1/instances", json=BODY))
    try:
        h1.worker.run_once()
    except Exception:  # noqa: BLE001
        pass
    assert h1.operation(op["id"])["status"] == "RUNNING"
    h1.client.__exit__(None, None, None)

    h1.clock.advance(6)
    h2 = make_harness(prov=provider)  # lifespan runs reconcile -> lease expired -> PENDING
    assert h2.operation(op["id"])["status"] == "PENDING"
    h2.drain()
    assert h2.operation(op["id"])["status"] == "SUCCEEDED"
    assert h2.instance(op["instance_id"])["state"] == "RUNNING"


def test_host_config_changes_apply_on_restart_without_losing_reservations(
    make_harness: MakeHarness, provider
) -> None:  # type: ignore[no-untyped-def]
    h1 = make_harness(hosts=[HostSpec(name="host-1", vcpus=4, memory_mib=4096)])
    h1.create(vcpus=4, memory_mib=4096)
    h1.client.__exit__(None, None, None)
    h2 = make_harness(
        hosts=[
            HostSpec(name="host-1", vcpus=8, memory_mib=8192),
            HostSpec(name="host-2", vcpus=2, memory_mib=2048),
        ],
        prov=provider,
    )
    cap = h2.capacity()
    assert (
        cap["total_vcpus"] == 10 and cap["reserved_vcpus"] == 4 and cap["active_reservations"] == 1
    )
    assert len(cap["hosts"]) == 2
