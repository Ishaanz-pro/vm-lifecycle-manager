"""Scheduler unit tests: placement policy and accounting, exercised directly on sessions."""

import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from vmlm.db.models import Host, HostStatus, Instance, Reservation
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.errors import DoubleAllocation, InsufficientCapacity
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.states import DesiredState, VMState


@pytest.fixture
def sf(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(db_url)
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def add_host(
    s: Session, name: str, vcpus: int, mem: int, status: HostStatus = HostStatus.READY
) -> Host:
    host = Host(
        id=str(uuid.uuid4()), name=name, total_vcpus=vcpus, total_memory_mib=mem, status=status
    )
    s.add(host)
    s.flush()
    return host


def add_instance(s: Session, instance_id: str) -> None:
    s.add(
        Instance(
            id=instance_id,
            name="x",
            vcpus=1,
            memory_mib=1,
            state=VMState.RUNNING,
            desired_state=DesiredState.RUNNING,
            provider_id=f"vm-{instance_id}",
            version=1,
        )
    )
    s.flush()


Hosts = Callable[[Session], None]


def test_best_fit_picks_host_with_least_leftover_memory(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "big", 32, 64_000)
        add_host(s, "snug", 8, 10_000)
        add_instance(s, "a")
        chosen = sched.place(s, "a", 2, 8_000)
        assert chosen.name == "snug"  # 2_000 left over beats 56_000 left over
        add_instance(s, "b")
        assert sched.place(s, "b", 2, 8_000).name == "big"  # snug is now too small


def test_best_fit_ties_break_on_vcpus_then_name(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "b-host", 8, 1000)
        add_host(s, "a-host", 8, 1000)
        add_host(s, "tight-cpu", 4, 1000)
        add_instance(s, "x")
        assert sched.place(s, "x", 2, 500).name == "tight-cpu"  # least vCPU left over
        add_instance(s, "y")
        assert sched.place(s, "y", 2, 500).name == "tight-cpu"  # 0 MiB left over: best fit
        add_instance(s, "z")
        assert sched.place(s, "z", 2, 500).name == "a-host"  # full tie: alphabetical


@pytest.mark.parametrize("status", [HostStatus.DRAINING, HostStatus.UNHEALTHY])
def test_non_ready_hosts_are_not_eligible(sf: sessionmaker[Session], status: HostStatus) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "down", 64, 64_000, status)
        add_instance(s, "a")
        with pytest.raises(InsufficientCapacity) as exc:
            sched.place(s, "a", 1, 1)
        assert exc.value.details["reason"] == "no_eligible_hosts"
        cap = sched.snapshot(s)
        assert cap.total_vcpus == 64 and cap.available_vcpus == 0  # not counted as available


@pytest.mark.parametrize("vcpus,memory", [(5, 100), (1, 700)])
def test_rejects_when_either_dimension_would_overflow(
    sf: sessionmaker[Session], vcpus: int, memory: int
) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h", 8, 1000)
        add_instance(s, "a")
        sched.place(s, "a", 4, 400)
        add_instance(s, "b")
        with pytest.raises(InsufficientCapacity) as exc:
            sched.place(s, "b", vcpus, memory)
        assert exc.value.details == {
            "reason": "insufficient_capacity",
            "requested_vcpus": vcpus,
            "requested_memory_mib": memory,
            "available_vcpus": 4,
            "available_memory_mib": 600,
        }
        assert sched.snapshot(s).active_reservations == 1


def test_exact_fit_is_allowed(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h", 4, 100)
        add_instance(s, "a")
        sched.place(s, "a", 4, 100)
        snap = sched.snapshot(s)
        assert snap.available_vcpus == 0 and snap.available_memory_mib == 0


def test_capacity_is_tracked_per_host(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h1", 8, 1000)
        add_host(s, "h2", 8, 1000)
        add_instance(s, "a")
        sched.place(s, "a", 8, 1000)  # fills one host completely
        add_instance(s, "b")
        sched.place(s, "b", 8, 1000)  # must land on the other
        by_name = {h.name: h for h in sched.host_capacities(s)}
        assert by_name["h1"].available_vcpus == 0 and by_name["h2"].available_vcpus == 0
        assert {by_name["h1"].active_reservations, by_name["h2"].active_reservations} == {1}
        add_instance(s, "c")
        with pytest.raises(InsufficientCapacity):
            sched.place(s, "c", 1, 1)


def test_double_allocation_rejected(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h", 8, 1000)
        add_instance(s, "a")
        sched.place(s, "a", 1, 10)
        with pytest.raises(DoubleAllocation):
            sched.place(s, "a", 1, 10)
        assert sched.snapshot(s).reserved_vcpus == 1


def test_double_release_is_a_noop(sf: sessionmaker[Session]) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h", 8, 1000)
        add_instance(s, "a")
        sched.place(s, "a", 2, 10)
        assert sched.release(s, "a") is True
        assert sched.release(s, "a") is False
        assert sched.snapshot(s).reserved_vcpus == 0


@pytest.mark.parametrize("vcpus,memory", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_non_positive_requests_rejected(sf: sessionmaker[Session], vcpus: int, memory: int) -> None:
    sched = Scheduler()
    with sf() as s, s.begin():
        add_host(s, "h", 8, 1000)
        add_instance(s, "a")
        with pytest.raises(ValueError):
            sched.place(s, "a", vcpus, memory)


def test_database_constraints_block_non_positive_rows(sf: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), sf() as s, s.begin():
        host = add_host(s, "h", 8, 1000)
        add_instance(s, "a")
        s.add(Reservation(instance_id="a", host_id=host.id, vcpus=0, memory_mib=5))
    with pytest.raises(IntegrityError), sf() as s, s.begin():
        add_host(s, "bad", 0, 1000)
