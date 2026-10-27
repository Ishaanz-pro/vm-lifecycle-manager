"""Worker semantics: retries, backoff, timeouts, leases, crashes, stale outcomes."""

from datetime import datetime

from tests.conftest import BODY, Harness, MakeHarness
from vmlm.domain.worker import Worker


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- retry classification ----------


def test_transient_failure_is_retried_then_succeeds(h: Harness) -> None:
    h.provider.fail_next("provision", times=2, retryable=True)
    op = h.submit(h.post("/v1/instances", json=BODY))

    assert h.worker.run_once() is True
    after_first = h.operation(op["id"])
    assert (after_first["status"], after_first["attempts"], after_first["error_kind"]) == (
        "PENDING",
        1,
        "transient",
    )
    assert h.instance(op["instance_id"])["state"] == "PROVISIONING"  # still in flight
    assert h.capacity()["reserved_vcpus"] == 2  # capacity held across retries

    assert h.drain() == 2  # attempt 2 fails, attempt 3 succeeds
    done = h.operation(op["id"])
    assert (done["status"], done["attempts"], done["error"]) == ("SUCCEEDED", 3, None)
    assert h.instance(op["instance_id"])["state"] == "RUNNING"
    retries = [
        e for e in h.events(op["instance_id"]) if e["event_type"] == "operation_retry_scheduled"
    ]
    assert len(retries) == 2 and "attempt 1/3" in retries[0]["detail"]
    assert [c[0] for c in h.provider.calls] == ["provision"] * 3  # one provider call per attempt


def test_attempts_exhausted_fails_and_rolls_back(h: Harness) -> None:
    h.provider.fail_next("provision", times=5, retryable=True)
    op = h.submit(h.post("/v1/instances", json=BODY))
    assert h.drain() == 3  # max_attempts
    done = h.operation(op["id"])
    assert (done["status"], done["attempts"], done["error_kind"]) == ("FAILED", 3, "transient")
    assert h.instance(op["instance_id"])["state"] == "FAILED"
    assert h.capacity()["reserved_vcpus"] == 0
    assert h.provider.pending_failures() == {"provision": 2}  # no 4th call was made


def test_permanent_failure_is_not_retried(h: Harness) -> None:
    h.provider.fail_next("provision", retryable=False)
    op = h.submit(h.post("/v1/instances", json=BODY))
    assert h.drain() == 1
    done = h.operation(op["id"])
    assert (done["status"], done["attempts"], done["error_kind"]) == ("FAILED", 1, "permanent")


def test_backoff_is_exponential_capped_and_jittered(make_harness: MakeHarness) -> None:
    h = make_harness(retry_base_delay_s=1.0, retry_max_delay_s=3.0, max_attempts=4)
    h.provider.fail_next("provision", times=3, retryable=True)
    op = h.submit(h.post("/v1/instances", json=BODY))
    ceilings = [1.0, 2.0, 3.0]  # 1*2^0, 1*2^1, min(3, 1*2^2)
    for attempt, ceiling in enumerate(ceilings, start=1):
        assert h.worker.run_once() is True
        o = h.operation(op["id"])
        assert o["attempts"] == attempt and o["status"] == "PENDING"
        delay = (ts(o["next_attempt_at"]) - h.clock()).total_seconds()
        assert 0 <= delay <= ceiling
        assert h.worker.run_once() is False  # not due yet (unless delay == 0)
        h.clock.advance(ceiling)
    assert h.worker.run_once() is True
    assert h.operation(op["id"])["status"] == "SUCCEEDED"


def test_provider_timeout_is_transient(make_harness: MakeHarness) -> None:
    h = make_harness(provider_timeout_s=0.05)
    h.provider.set_latency("provision", 0.3)
    op = h.submit(h.post("/v1/instances", json=BODY))
    assert h.worker.run_once() is True
    o = h.operation(op["id"])
    assert (
        o["status"] == "PENDING"
        and o["error_kind"] == "transient"
        and "exceeded 0.05s" in o["error"]
    )
    h.provider.set_latency("provision", 0)
    h.drain()
    assert h.operation(op["id"])["status"] == "SUCCEEDED"
    # the timed-out call completed in the background; a second provision was harmless (idempotent)
    assert h.provider.snapshot() == {h.instance(op["instance_id"])["provider_id"]: "RUNNING"}


# ---- leases: crash and DB-error recovery ----------


def test_worker_crash_leaves_lease_which_expires_and_requeues(make_harness: MakeHarness) -> None:
    h = make_harness(provider_timeout_s=1, worker_lease_s=30)
    h.injector.arm("worker_crash")
    op = h.submit(h.post("/v1/instances", json=BODY))
    try:
        h.worker.run_once()
    except Exception as exc:  # noqa: BLE001
        assert "injected crash" in str(exc)
    stuck = h.operation(op["id"])
    assert stuck["status"] == "RUNNING" and stuck["attempts"] == 1
    assert h.provider.snapshot() != {}  # provider side effect happened before the "crash"
    assert h.instance(op["instance_id"])["state"] == "PROVISIONING"

    assert h.worker.run_once() is False  # still leased: nobody else may take it
    assert h.reconciler.run_once().requeued_operations == 0  # lease not yet expired
    h.clock.advance(31)
    assert h.reconciler.run_once().requeued_operations == 1
    requeued = h.operation(op["id"])
    assert requeued["status"] == "PENDING" and "lease expired" in requeued["error"]
    assert h.drain() == 1
    done = h.operation(op["id"])
    assert done["status"] == "SUCCEEDED" and done["attempts"] == 2
    assert [c[0] for c in h.provider.calls] == ["provision", "provision"]  # re-run was idempotent
    assert any(e["event_type"] == "operation_requeued" for e in h.events(op["instance_id"]))


def test_database_error_on_outcome_commit_is_recovered_by_lease(make_harness: MakeHarness) -> None:
    h = make_harness(provider_timeout_s=1, worker_lease_s=10)
    inst = h.create()
    h.injector.arm("db_error")
    op = h.submit(h.post(f"/v1/instances/{inst['id']}/stop"))
    h.worker.run_once()
    assert h.operation(op["id"])["status"] == "RUNNING"
    assert (
        h.instance(inst["id"])["state"] == "STOPPING"
    )  # outcome was rolled back, not half-applied
    h.clock.advance(11)
    h.reconciler.run_once()
    h.drain()
    assert h.operation(op["id"])["status"] == "SUCCEEDED"
    assert h.instance(inst["id"])["state"] == "STOPPED"


def test_lease_expiry_after_last_attempt_fails_the_operation(make_harness: MakeHarness) -> None:
    h = make_harness(provider_timeout_s=1, worker_lease_s=5, max_attempts=1)
    h.injector.arm("worker_crash")
    op = h.submit(h.post("/v1/instances", json=BODY))
    try:
        h.worker.run_once()
    except Exception:  # noqa: BLE001
        pass
    h.clock.advance(6)
    h.reconciler.run_once()
    done = h.operation(op["id"])
    assert done["status"] == "FAILED" and "last permitted attempt" in done["error"]
    assert h.instance(op["instance_id"])["state"] == "FAILED"
    assert h.capacity()["reserved_vcpus"] == 0


def test_stale_outcome_from_a_dead_worker_is_dropped(make_harness: MakeHarness) -> None:
    """A stalls past its lease; B takes over and finishes; A's late outcome must not apply."""
    h = make_harness(provider_timeout_s=1, worker_lease_s=5)
    op = h.submit(h.post("/v1/instances", json=BODY))
    a = h.worker
    claimed = a.claim_next()
    assert claimed is not None and claimed.claimed_by == a.worker_id

    h.clock.advance(6)
    h.reconciler.run_once()  # lease expired -> PENDING
    b = Worker(
        h.app.state.session_factory, h.provider, a._scheduler, worker_id="worker-b", clock=h.clock
    )
    assert b.run_once() is True
    assert h.operation(op["id"])["status"] == "SUCCEEDED"

    a.execute(claimed)  # A finally finishes: provider call is idempotent, outcome is stale
    o = h.operation(op["id"])
    assert o["status"] == "SUCCEEDED" and o["attempts"] == 2
    assert h.instance(op["instance_id"])["version"] == 2  # not bumped twice
    assert [
        e["to_state"] for e in h.events(op["instance_id"]) if e["event_type"] == "state_changed"
    ] == ["RUNNING"]
    b.close()


def test_claim_order_is_oldest_due_first(h: Harness) -> None:
    ops = [
        h.submit(
            h.post("/v1/instances", json={**BODY, "name": f"n{i}", "vcpus": 1, "memory_mib": 64})
        )
        for i in range(3)
    ]
    for expected in ops:
        claimed = h.worker.claim_next()
        assert claimed is not None and claimed.id == expected["id"]
