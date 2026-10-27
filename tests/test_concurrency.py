"""Concurrency tests use independent services/workers (separate engines) on one SQLite file,
which approximates multiple processes on one machine. They say nothing about multi-host."""

import random
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from vmlm.config import HostSpec
from vmlm.db.models import AuditEvent, Instance, Operation, Reservation
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.errors import IdempotencyConflict, InsufficientCapacity, InvalidTransition
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.service import LifecycleService
from vmlm.domain.states import OperationAction, OperationStatus, VMState
from vmlm.domain.worker import Worker
from vmlm.providers.simulated import SimulatedProvider


class Node:
    """One 'process': its own engine, service and worker over the shared database file."""

    def __init__(self, db_url: str, provider: SimulatedProvider, name: str) -> None:
        self.engine = make_engine(db_url)
        init_db(self.engine)
        self.sf: sessionmaker[Session] = make_session_factory(self.engine)
        self.scheduler = Scheduler()
        self.service = LifecycleService(self.sf, self.scheduler)
        self.worker = Worker(
            self.sf, provider, self.scheduler, worker_id=name, retry_base_delay_s=0
        )

    def close(self) -> None:
        self.worker.close()
        self.engine.dispose()


@pytest.fixture
def nodes(
    db_url: str, provider: SimulatedProvider
) -> Iterator[Callable[[int, list[HostSpec]], list[Node]]]:
    made: list[Node] = []

    def _make(n: int, hosts: list[HostSpec]) -> list[Node]:
        for i in range(n):
            made.append(Node(db_url, provider, f"node-{i}"))
        made[0].service.seed_hosts(hosts)
        return made

    yield _make
    for node in made:
        node.close()


def run_all(fns: list[Callable[[], object]]) -> list[object]:
    """Run callables simultaneously (barrier-aligned); return each result or raised exception."""
    barrier = threading.Barrier(len(fns))

    def wrap(fn: Callable[[], object]) -> object:
        barrier.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            return exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(wrap, fns))


def test_concurrent_creates_never_overcommit_a_host(nodes) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(2, [HostSpec(name="h", vcpus=16, memory_mib=16384)])
    results = run_all(
        [(lambda i=i: ns[i % 2].service.create_instance(f"vm-{i}", 1, 2048)) for i in range(24)]
    )
    ok = [r for r in results if isinstance(r, tuple)]
    rejected = [r for r in results if isinstance(r, InsufficientCapacity)]
    assert len(ok) == 8 and len(rejected) == 16  # memory is the binding constraint
    cap = ns[0].service.capacity()
    assert (cap.reserved_memory_mib, cap.available_memory_mib, cap.active_reservations) == (
        16384,
        0,
        8,
    )
    with ns[0].sf() as s:
        assert s.scalar(select(func.count()).select_from(Instance)) == 8  # rejected left no rows
        assert s.scalar(select(func.count()).select_from(Operation)) == 8


def test_concurrent_mixed_sizes_over_two_hosts_stay_within_capacity(nodes) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(
        3,
        [
            HostSpec(name="a", vcpus=10, memory_mib=10_000),
            HostSpec(name="b", vcpus=6, memory_mib=5_000),
        ],
    )
    rng = random.Random(1234)
    sizes = [(rng.randint(1, 4), rng.randint(500, 4000)) for _ in range(30)]
    results = run_all(
        [
            (lambda i=i, c=c, m=m: ns[i % 3].service.create_instance(f"vm-{i}", c, m))
            for i, (c, m) in enumerate(sizes)
        ]
    )
    assert all(isinstance(r, tuple | InsufficientCapacity) for r in results)
    for host in ns[0].service.capacity().hosts:
        assert host.reserved_vcpus <= host.total_vcpus
        assert host.reserved_memory_mib <= host.total_memory_mib
    granted = [sizes[i] for i, r in enumerate(results) if isinstance(r, tuple)]
    assert ns[0].service.capacity().reserved_vcpus == sum(c for c, _ in granted)


def test_two_workers_execute_each_operation_exactly_once(
    nodes, provider: SimulatedProvider
) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(2, [HostSpec(name="h", vcpus=64, memory_mib=64_000)])
    ops = [ns[0].service.create_instance(f"vm-{i}", 1, 100)[0] for i in range(20)]
    counts = run_all([ns[0].worker.run_until_idle, ns[1].worker.run_until_idle])
    assert sum(c for c in counts if isinstance(c, int)) == 20
    assert sorted(pid for _, pid in provider.calls) == sorted(f"vm-{op.instance_id}" for op in ops)
    with ns[0].sf() as s:
        statuses = s.scalars(select(Operation.status)).all()
        assert statuses == [OperationStatus.SUCCEEDED] * 20
        states = s.scalars(select(Instance.state)).all()
        assert states == [VMState.RUNNING] * 20
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "state_changed")
            )
            == 20
        )


def test_concurrent_conflicting_actions_on_one_instance_admit_exactly_one(
    nodes, provider: SimulatedProvider
) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(2, [HostSpec(name="h", vcpus=8, memory_mib=8000)])
    op, _ = ns[0].service.create_instance("vm", 1, 100)
    ns[0].worker.run_until_idle()
    actions = [
        OperationAction.STOP,
        OperationAction.TERMINATE,
        OperationAction.STOP,
        OperationAction.TERMINATE,
    ]
    results = run_all(
        [
            (lambda n=n, a=a: n.service.request_action(op.instance_id, a))
            for n, a in zip(ns * 2, actions, strict=True)
        ]
    )
    accepted = [r for r in results if isinstance(r, tuple)]
    rejected = [r for r in results if isinstance(r, InvalidTransition)]
    assert len(accepted) == 1 and len(rejected) == 3
    run_all([ns[0].worker.run_until_idle, ns[1].worker.run_until_idle])
    winner = accepted[0][0].action
    mutating = [c for c in provider.calls if c[0] in {"stop", "delete"}]
    assert mutating == [
        ("stop" if winner is OperationAction.STOP else "delete", f"vm-{op.instance_id}")
    ]
    with ns[0].sf() as s:
        assert s.scalar(select(func.count()).select_from(Reservation)) == (
            1 if winner is OperationAction.STOP else 0
        )


def test_concurrent_idempotent_creates_make_one_instance(nodes) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(2, [HostSpec(name="h", vcpus=8, memory_mib=8000)])
    results = run_all(
        [(lambda n=n: n.service.create_instance("vm", 1, 128, "same-key")) for n in ns * 5]
    )
    assert all(isinstance(r, tuple) for r in results)
    assert len({r[0].id for r in results if isinstance(r, tuple)}) == 1
    assert sum(r[1] for r in results if isinstance(r, tuple)) == 1
    assert ns[0].service.capacity().active_reservations == 1


def test_concurrent_same_key_different_bodies_admit_one(nodes) -> None:  # type: ignore[no-untyped-def]
    ns = nodes(2, [HostSpec(name="h", vcpus=8, memory_mib=8000)])
    results = run_all(
        [
            (lambda n=n, v=v: n.service.create_instance("vm", v, 128, "k"))
            for n, v in zip(ns * 3, [1, 2, 3, 1, 2, 3], strict=True)
        ]
    )
    created = [r for r in results if isinstance(r, tuple) and r[1]]
    assert len(created) == 1
    assert all(isinstance(r, tuple | IdempotencyConflict) for r in results)
