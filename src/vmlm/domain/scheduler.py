"""Multi-host capacity accounting and placement.

Policy: BEST FIT by memory. Among READY hosts where both vCPU and memory fit, choose the one
with the least memory left over after placement; break ties by least vCPU left over, then
by host name (deterministic). Best fit packs instances tightly, which keeps large slots free
for large requests at the cost of spreading load less evenly than "least allocated".

Complexity per placement: one GROUP BY over live reservations, O(R), plus an O(H) scan of
hosts. H and R are small for a single control plane; a per-host running-total column would
make it O(H) only.

Concurrency: place()/release() MUST run inside a write transaction. On SQLite that is
BEGIN IMMEDIATE (db/session.py), which serializes all writers. On PostgreSQL the host rows
are locked with SELECT ... FOR UPDATE so two placements on the same host serialize while
placements on different hosts may proceed. This class holds no in-process lock and no
in-memory counters, so state survives restarts and is shared by every worker/process on the
same database.
"""

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from vmlm.db.models import Host, HostStatus, Reservation
from vmlm.domain.errors import DoubleAllocation, InsufficientCapacity
from vmlm.observability.metrics import SCHEDULER_REJECTIONS


@dataclass(frozen=True)
class HostCapacity:
    host_id: str
    name: str
    status: HostStatus
    total_vcpus: int
    reserved_vcpus: int
    available_vcpus: int
    total_memory_mib: int
    reserved_memory_mib: int
    available_memory_mib: int
    active_reservations: int


@dataclass(frozen=True)
class ClusterCapacity:
    total_vcpus: int
    reserved_vcpus: int
    available_vcpus: int
    total_memory_mib: int
    reserved_memory_mib: int
    available_memory_mib: int
    active_reservations: int
    hosts: list[HostCapacity]


class Scheduler:
    def host_capacities(self, session: Session, lock: bool = False) -> list[HostCapacity]:
        hosts_q = select(Host).order_by(Host.name)
        if lock:
            hosts_q = hosts_q.with_for_update()  # no-op on SQLite; row locks on PostgreSQL
        hosts = session.scalars(hosts_q).all()
        usage = {
            host_id: (vcpus, memory, count)
            for host_id, vcpus, memory, count in session.execute(
                select(
                    Reservation.host_id,
                    func.coalesce(func.sum(Reservation.vcpus), 0),
                    func.coalesce(func.sum(Reservation.memory_mib), 0),
                    func.count(),
                ).group_by(Reservation.host_id)
            )
        }
        result = []
        for h in hosts:
            vcpus, memory, count = usage.get(h.id, (0, 0, 0))
            result.append(
                HostCapacity(
                    host_id=h.id,
                    name=h.name,
                    status=h.status,
                    total_vcpus=h.total_vcpus,
                    reserved_vcpus=vcpus,
                    available_vcpus=h.total_vcpus - vcpus,
                    total_memory_mib=h.total_memory_mib,
                    reserved_memory_mib=memory,
                    available_memory_mib=h.total_memory_mib - memory,
                    active_reservations=count,
                )
            )
        return result

    def snapshot(self, session: Session) -> ClusterCapacity:
        hosts = self.host_capacities(session)
        return ClusterCapacity(
            total_vcpus=sum(h.total_vcpus for h in hosts),
            reserved_vcpus=sum(h.reserved_vcpus for h in hosts),
            available_vcpus=sum(h.available_vcpus for h in hosts if h.status is HostStatus.READY),
            total_memory_mib=sum(h.total_memory_mib for h in hosts),
            reserved_memory_mib=sum(h.reserved_memory_mib for h in hosts),
            available_memory_mib=sum(
                h.available_memory_mib for h in hosts if h.status is HostStatus.READY
            ),
            active_reservations=sum(h.active_reservations for h in hosts),
            hosts=hosts,
        )

    def place(
        self, session: Session, instance_id: str, vcpus: int, memory_mib: int
    ) -> HostCapacity:
        """Pick a host (best fit) and reserve capacity on it. Raises InsufficientCapacity."""
        if vcpus <= 0 or memory_mib <= 0:
            raise ValueError("resource request must be positive")
        if session.get(Reservation, instance_id) is not None:
            raise DoubleAllocation(f"instance {instance_id} already holds a reservation")

        hosts = self.host_capacities(session, lock=True)
        eligible = [h for h in hosts if h.status is HostStatus.READY]
        fitting = [
            h
            for h in eligible
            if vcpus <= h.available_vcpus and memory_mib <= h.available_memory_mib
        ]
        if not fitting:
            reason = "no_eligible_hosts" if not eligible else "insufficient_capacity"
            SCHEDULER_REJECTIONS.inc(reason=reason)
            raise InsufficientCapacity(
                "no host has enough free capacity"
                if eligible
                else "no host is currently accepting placements",
                {
                    "reason": reason,
                    "requested_vcpus": vcpus,
                    "requested_memory_mib": memory_mib,
                    "available_vcpus": max((h.available_vcpus for h in eligible), default=0),
                    "available_memory_mib": max(
                        (h.available_memory_mib for h in eligible), default=0
                    ),
                },
            )
        chosen = min(
            fitting,
            key=lambda h: (h.available_memory_mib - memory_mib, h.available_vcpus - vcpus, h.name),
        )
        session.add(
            Reservation(
                instance_id=instance_id, host_id=chosen.host_id, vcpus=vcpus, memory_mib=memory_mib
            )
        )
        session.flush()
        return chosen

    def release(self, session: Session, instance_id: str) -> bool:
        """Release a reservation. Returns False if there was none (double release is a no-op)."""
        result = session.execute(delete(Reservation).where(Reservation.instance_id == instance_id))
        return result.rowcount == 1
