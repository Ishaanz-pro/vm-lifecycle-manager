"""Capacity accounting for one simulated host.

reserve()/release() MUST run inside a write transaction (BEGIN IMMEDIATE, see db/session.py).
That lock is what makes "sum, compare, insert" atomic; this class holds no in-process lock
and no in-memory counters, so state survives restarts and is shared by all workers on the
same database file. Cost: O(R) per call for R live reservations (a SUM scan); fine at
MVP scale, a running-total row would make it O(1).
"""

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from vmlm.db.models import Reservation
from vmlm.domain.errors import DoubleAllocation, InsufficientCapacity


@dataclass(frozen=True)
class CapacitySnapshot:
    total_vcpus: int
    reserved_vcpus: int
    available_vcpus: int
    total_memory_mib: int
    reserved_memory_mib: int
    available_memory_mib: int
    active_reservations: int


class Scheduler:
    def __init__(self, total_vcpus: int, total_memory_mib: int) -> None:
        if total_vcpus <= 0 or total_memory_mib <= 0:
            raise ValueError("host capacity must be positive")
        self._total_vcpus = total_vcpus
        self._total_memory_mib = total_memory_mib

    def snapshot(self, session: Session) -> CapacitySnapshot:
        vcpus, memory, count = session.execute(
            select(
                func.coalesce(func.sum(Reservation.vcpus), 0),
                func.coalesce(func.sum(Reservation.memory_mib), 0),
                func.count(),
            )
        ).one()
        return CapacitySnapshot(
            self._total_vcpus,
            vcpus,
            self._total_vcpus - vcpus,
            self._total_memory_mib,
            memory,
            self._total_memory_mib - memory,
            count,
        )

    def reserve(self, session: Session, instance_id: str, vcpus: int, memory_mib: int) -> None:
        if vcpus <= 0 or memory_mib <= 0:
            raise ValueError("resource request must be positive")
        if session.get(Reservation, instance_id) is not None:
            raise DoubleAllocation(f"instance {instance_id} already holds a reservation")
        cap = self.snapshot(session)
        if vcpus > cap.available_vcpus or memory_mib > cap.available_memory_mib:
            raise InsufficientCapacity(
                "host does not have enough free capacity",
                {
                    "requested_vcpus": vcpus,
                    "requested_memory_mib": memory_mib,
                    "available_vcpus": cap.available_vcpus,
                    "available_memory_mib": cap.available_memory_mib,
                },
            )
        session.add(Reservation(instance_id=instance_id, vcpus=vcpus, memory_mib=memory_mib))
        session.flush()

    def release(self, session: Session, instance_id: str) -> bool:
        """Release a reservation. Returns False if there was none (double release is a no-op)."""
        result = session.execute(delete(Reservation).where(Reservation.instance_id == instance_id))
        return result.rowcount == 1
