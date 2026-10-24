from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import ServiceKit
from vmlm.db.models import Instance, Reservation
from vmlm.domain.errors import DoubleAllocation, InsufficientCapacity
from vmlm.domain.states import VMState


def _add_instance(session, instance_id: str, vcpus: int = 1, memory_mib: int = 1) -> None:  # type: ignore[no-untyped-def]
    session.add(
        Instance(
            id=instance_id, name="x", vcpus=vcpus, memory_mib=memory_mib,
            state=VMState.RUNNING, provider_id=f"vm-{instance_id}", version=1,
        )
    )  # fmt: skip
    session.flush()


def test_reserve_tracks_total_reserved_available(make_service: Callable[..., ServiceKit]) -> None:
    kit = make_service(vcpus=8, memory_mib=1000)
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        kit.scheduler.reserve(s, "a", 3, 400)
        cap = kit.scheduler.snapshot(s)
    assert (cap.total_vcpus, cap.reserved_vcpus, cap.available_vcpus) == (8, 3, 5)
    assert (cap.total_memory_mib, cap.reserved_memory_mib, cap.available_memory_mib) == (
        1000, 400, 600,
    )  # fmt: skip
    assert cap.active_reservations == 1


@pytest.mark.parametrize("vcpus,memory", [(5, 100), (1, 700)])
def test_rejects_when_either_dimension_would_overflow(
    make_service: Callable[..., ServiceKit], vcpus: int, memory: int
) -> None:
    kit = make_service(vcpus=8, memory_mib=1000)
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        kit.scheduler.reserve(s, "a", 4, 400)
        _add_instance(s, "b")
        with pytest.raises(InsufficientCapacity):
            kit.scheduler.reserve(s, "b", vcpus, memory)
        assert kit.scheduler.snapshot(s).active_reservations == 1


def test_exact_fit_is_allowed(make_service: Callable[..., ServiceKit]) -> None:
    kit = make_service(vcpus=4, memory_mib=100)
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        kit.scheduler.reserve(s, "a", 4, 100)
        assert kit.scheduler.snapshot(s).available_vcpus == 0


def test_double_allocation_rejected(make_service: Callable[..., ServiceKit]) -> None:
    kit = make_service()
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        kit.scheduler.reserve(s, "a", 1, 10)
        with pytest.raises(DoubleAllocation):
            kit.scheduler.reserve(s, "a", 1, 10)
        assert kit.scheduler.snapshot(s).reserved_vcpus == 1


def test_double_release_is_a_noop(make_service: Callable[..., ServiceKit]) -> None:
    kit = make_service()
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        kit.scheduler.reserve(s, "a", 2, 10)
        assert kit.scheduler.release(s, "a") is True
        assert kit.scheduler.release(s, "a") is False
        assert kit.scheduler.snapshot(s).reserved_vcpus == 0


@pytest.mark.parametrize("vcpus,memory", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_non_positive_requests_rejected(
    make_service: Callable[..., ServiceKit], vcpus: int, memory: int
) -> None:
    kit = make_service()
    with kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        with pytest.raises(ValueError):
            kit.scheduler.reserve(s, "a", vcpus, memory)


def test_database_constraint_blocks_non_positive_reservation(
    make_service: Callable[..., ServiceKit],
) -> None:
    kit = make_service()
    with pytest.raises(IntegrityError), kit.session_factory() as s, s.begin():
        _add_instance(s, "a")
        s.add(Reservation(instance_id="a", vcpus=0, memory_mib=5))


def test_invalid_host_capacity_rejected() -> None:
    from vmlm.domain.scheduler import Scheduler

    with pytest.raises(ValueError):
        Scheduler(0, 100)
