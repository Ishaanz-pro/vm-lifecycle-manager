"""Concurrency tests use two independent services (separate engines/connections) on one DB
file, which approximates multiple workers. They prove safety on a single machine's SQLite
file; they say nothing about multi-host deployments."""

import random
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from tests.conftest import ServiceKit
from vmlm.db.models import AuditEvent, Instance, Reservation
from vmlm.domain.errors import ConcurrentModification, InsufficientCapacity, InvalidTransition
from vmlm.domain.states import VMState
from vmlm.providers.simulated import SimulatedProvider


def _run_all(fns: list[Callable[[], object]]) -> list[object]:
    """Run callables simultaneously (barrier-aligned); return result or raised exception."""
    barrier = threading.Barrier(len(fns))

    def wrap(fn: Callable[[], object]) -> object:
        barrier.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            return exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(wrap, fns))


def test_concurrent_creates_never_overcommit(
    make_service: Callable[..., ServiceKit],
) -> None:
    kits = [make_service(vcpus=16, memory_mib=16384) for _ in range(2)]  # two "workers"
    fns = [(lambda i=i: kits[i % 2].service.create_instance(f"vm-{i}", 1, 2048)) for i in range(24)]
    results = _run_all(fns)

    ok = [r for r in results if isinstance(r, tuple)]
    rejected = [r for r in results if isinstance(r, InsufficientCapacity)]
    assert len(ok) == 8  # 16384 MiB / 2048 MiB: memory is the binding constraint
    assert len(rejected) == 16
    assert len(ok) + len(rejected) == 24  # nothing else went wrong

    cap = kits[0].service.capacity()
    assert cap.reserved_memory_mib == 16384 and cap.available_memory_mib == 0
    assert cap.reserved_vcpus == 8 and cap.active_reservations == 8
    with kits[0].session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Instance)) == 8  # rejected left no rows


def test_concurrent_mixed_sizes_stay_within_capacity(
    make_service: Callable[..., ServiceKit],
) -> None:
    kits = [make_service(vcpus=10, memory_mib=10_000) for _ in range(3)]
    rng = random.Random(1234)
    sizes = [(rng.randint(1, 4), rng.randint(500, 4000)) for _ in range(30)]
    fns = [
        (lambda i=i, c=c, m=m: kits[i % 3].service.create_instance(f"vm-{i}", c, m))
        for i, (c, m) in enumerate(sizes)
    ]
    results = _run_all(fns)

    assert all(isinstance(r, tuple | InsufficientCapacity) for r in results)
    granted = [sizes[i] for i, r in enumerate(results) if isinstance(r, tuple)]
    cap = kits[0].service.capacity()
    assert cap.reserved_vcpus == sum(c for c, _ in granted) <= 10
    assert cap.reserved_memory_mib == sum(m for _, m in granted) <= 10_000


def test_concurrent_stop_of_same_instance_runs_provider_once(
    make_service: Callable[..., ServiceKit], provider: SimulatedProvider
) -> None:
    kits = [make_service(), make_service()]
    inst, _ = kits[0].service.create_instance("vm", 1, 128)
    results = _run_all(
        [lambda k=k: k.service.stop_instance(inst.id) for k in kits for _ in range(2)]
    )

    ok = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, InvalidTransition | ConcurrentModification)]
    assert len(ok) == 1 and len(errors) == 3
    assert [c for c in provider.calls if c[0] == "stop"] == [("stop", inst.provider_id)]
    assert kits[0].service.get_instance(inst.id).state is VMState.STOPPED


def test_concurrent_delete_releases_exactly_once(
    make_service: Callable[..., ServiceKit],
) -> None:
    kits = [make_service(), make_service()]
    inst, _ = kits[0].service.create_instance("vm", 2, 256)
    results = _run_all(
        [lambda k=k: k.service.terminate_instance(inst.id) for k in kits for _ in range(3)]
    )

    assert sum(not isinstance(r, Exception) for r in results) == 1
    with kits[0].session_factory() as s:
        released = s.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.instance_id == inst.id, AuditEvent.event_type == "resources_released")
        )
        assert released == 1
        assert s.scalar(select(func.count()).select_from(Reservation)) == 0


def test_concurrent_duplicate_idempotent_creates_make_one_instance(
    make_service: Callable[..., ServiceKit],
) -> None:
    kits = [make_service(), make_service()]
    results = _run_all(
        [
            lambda k=k: k.service.create_instance("vm", 1, 128, "same-key")
            for k in kits
            for _ in range(5)
        ]
    )

    assert all(isinstance(r, tuple) for r in results)
    assert len({r[0].id for r in results if isinstance(r, tuple)}) == 1
    assert sum(r[1] for r in results if isinstance(r, tuple)) == 1  # exactly one real creation
    assert kits[0].service.capacity().active_reservations == 1
