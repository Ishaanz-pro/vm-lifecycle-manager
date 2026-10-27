"""Regression tests for bugs found after the initial implementation. Each names the bug."""

import threading

import pytest
from sqlalchemy import text

from tests.conftest import BODY, MakeHarness
from vmlm.api.routers import accepted_status
from vmlm.db.models import Instance
from vmlm.domain.core import read_tx, tx


@pytest.mark.parametrize(
    "created,waited,done,expected",
    [
        (True, False, False, 202),
        (True, False, True, 202),  # BUG: returned 200 when a fast worker won the race
        (True, True, True, 200),
        (True, True, False, 202),
        (False, False, False, 200),
        (False, True, True, 200),
    ],
)
def test_bug_new_request_without_wait_must_be_202(
    created: bool, waited: bool, done: bool, expected: int
) -> None:
    """Found by scripts/loadtest.py: with a 10 ms worker poll, some creates finished before
    the response was built and came back 200, indistinguishable from an idempotent replay."""
    assert accepted_status(created, waited, done) == expected


def test_bug_new_request_already_finished_still_returns_202(make_harness: MakeHarness) -> None:
    """End-to-end version: force the race deterministically by finishing the operation
    inside the request, before the handler reads it back."""
    h = make_harness()
    original = h.service.create_instance

    def create_then_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        h.worker.run_until_idle()
        return result

    h.service.create_instance = create_then_drain  # type: ignore[method-assign]
    resp = h.post("/v1/instances", json=BODY)
    assert resp.json()["status"] == "SUCCEEDED"
    assert resp.status_code == 202


def test_bug_reads_must_not_take_the_write_lock(make_harness: MakeHarness) -> None:
    """Found by scripts/loadtest.py: every read used BEGIN IMMEDIATE, so polling clients
    queued behind writers (p99 2.7 s at concurrency 8). A read must proceed while a write
    transaction holds the lock, and must see the last committed snapshot."""
    h = make_harness()
    inst = h.create()
    sf = h.app.state.session_factory
    writer_has_lock = threading.Event()
    reader_done = threading.Event()
    observed: list[str] = []

    def writer() -> None:
        with tx(sf) as s:
            s.get(Instance, inst["id"]).name = "renamed"  # type: ignore[union-attr]
            s.flush()
            writer_has_lock.set()
            assert reader_done.wait(5), "reader blocked behind the writer"

    t = threading.Thread(target=writer)
    t.start()
    assert writer_has_lock.wait(5)
    with read_tx(sf) as s:
        observed.append(s.get(Instance, inst["id"]).name)  # type: ignore[union-attr]
    reader_done.set()
    t.join(5)
    assert observed == ["web-1"]  # snapshot: the uncommitted rename is invisible
    assert h.instance(inst["id"])["name"] == "renamed"


def test_read_tx_cannot_commit_a_write(make_harness: MakeHarness) -> None:
    h = make_harness()
    inst = h.create()
    sf = h.app.state.session_factory
    with read_tx(sf) as s:
        s.get(Instance, inst["id"]).name = "sneaky"  # type: ignore[union-attr]
    assert h.instance(inst["id"])["name"] == "web-1"


def test_writes_still_use_begin_immediate(make_harness: MakeHarness) -> None:
    """Guard the other half of the split: a second writer must wait for the first."""
    h = make_harness()
    sf = h.app.state.session_factory
    first_holding = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def first() -> None:
        with tx(sf) as s:
            s.execute(text("SELECT 1"))
            first_holding.set()
            release.wait(5)
            order.append("first-commit")

    t = threading.Thread(target=first)
    t.start()
    assert first_holding.wait(5)
    threading.Timer(0.3, release.set).start()
    with tx(sf) as s:  # blocks on BEGIN IMMEDIATE until `first` commits
        s.execute(text("SELECT 1"))
        order.append("second-begin")
    t.join(5)
    assert order == ["first-commit", "second-begin"]
