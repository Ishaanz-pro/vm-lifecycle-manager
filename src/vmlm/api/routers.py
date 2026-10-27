"""Thin HTTP handlers: parse input, call the service, shape output.

Asynchronous contract: every mutating call returns 202 Accepted with an Operation and a
`Location` header pointing at /v1/operations/{id}. Poll that, or pass `?wait=<seconds>`
(max 30) to have the server poll for you; the response is then 200 if the operation finished
in time and 202 if it is still running. An idempotent replay returns 200 with the original
operation.
"""

import platform
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import PlainTextResponse

from vmlm import sysinfo
from vmlm.api.deps import (
    get_injector,
    get_provider,
    get_reconciler,
    get_service,
    get_settings,
    get_worker,
    require_admin_key,
    require_api_key,
    require_failure_injection,
)
from vmlm.api.schemas import (
    CapacityOut,
    CreateInstanceRequest,
    ErrorResponse,
    EventPage,
    FailureRequest,
    HeartbeatRequest,
    HostOut,
    HostPage,
    HostStatusRequest,
    InstanceOut,
    InstancePage,
    OperationOut,
    OperationPage,
)
from vmlm.config import Settings
from vmlm.db.models import HostStatus
from vmlm.domain.failures import FailureInjector
from vmlm.domain.reconciler import Reconciler
from vmlm.domain.service import LifecycleService
from vmlm.domain.states import TERMINAL_OPERATION_STATUSES, OperationAction, VMState
from vmlm.domain.worker import Worker
from vmlm.observability.metrics import render_all
from vmlm.providers.base import VMProvider
from vmlm.providers.simulated import SimulatedProvider

Service = Annotated[LifecycleService, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
Wait = Annotated[float, Query(ge=0, le=30, description="seconds to wait for completion")]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)]

ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=ERRORS)
admin = APIRouter(
    prefix="/v1/admin", dependencies=[Depends(require_admin_key)], responses=ERRORS, tags=["admin"]
)
debug = APIRouter(
    prefix="/v1/debug",
    dependencies=[Depends(require_admin_key), Depends(require_failure_injection)],
    responses=ERRORS,
    tags=["debug (dev only)"],
)
public = APIRouter(tags=["health"])


def accepted_status(created: bool, waited: bool, done: bool) -> int:
    """HTTP status for a mutating request.

    * replay of an earlier request (idempotency key)          -> 200
    * new request, client did not ask to wait                 -> 202, even if a fast worker
      already finished it: the status must describe the request, not a race
    * new request with ?wait=, finished within the wait       -> 200
    * new request with ?wait=, still running when wait expired -> 202
    """
    if not created:
        return 200
    return 200 if (waited and done) else 202


def _accepted(
    service: LifecycleService, op_id: str, created: bool, wait: float, response: Response
) -> OperationOut:
    op = service.wait_for_operation(op_id, wait) if wait > 0 else service.get_operation(op_id)
    done = op.status in TERMINAL_OPERATION_STATUSES
    response.status_code = accepted_status(created, wait > 0, done)
    response.headers["Location"] = f"/v1/operations/{op.id}"
    return OperationOut.model_validate(op)


# ---- instances ----------------------------------------------------------------------------


@router.post("/instances", response_model=OperationOut, status_code=202, tags=["instances"])
def create_instance(
    body: CreateInstanceRequest,
    response: Response,
    service: Service,
    idempotency_key: IdemKey = None,
    wait: Wait = 0,
) -> OperationOut:
    op, created = service.create_instance(body.name, body.vcpus, body.memory_mib, idempotency_key)
    return _accepted(service, op.id, created, wait, response)


@router.get("/instances", response_model=InstancePage, tags=["instances"])
def list_instances(
    service: Service, state: VMState | None = None, limit: Limit = 50, offset: Offset = 0
) -> InstancePage:
    items, total = service.list_instances(state, limit, offset)
    return InstancePage(
        items=[InstanceOut.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/instances/{instance_id}", response_model=InstanceOut, tags=["instances"])
def get_instance(instance_id: str, service: Service) -> InstanceOut:
    return InstanceOut.model_validate(service.get_instance(instance_id))


def _action(action: OperationAction):  # type: ignore[no-untyped-def]
    def handler(
        instance_id: str,
        response: Response,
        service: Service,
        idempotency_key: IdemKey = None,
        wait: Wait = 0,
    ) -> OperationOut:
        op, created = service.request_action(instance_id, action, idempotency_key)
        return _accepted(service, op.id, created, wait, response)

    handler.__name__ = f"{action.value}_instance"
    return handler


router.post(
    "/instances/{instance_id}/start",
    response_model=OperationOut,
    status_code=202,
    tags=["instances"],
)(_action(OperationAction.START))
router.post(
    "/instances/{instance_id}/stop",
    response_model=OperationOut,
    status_code=202,
    tags=["instances"],
)(_action(OperationAction.STOP))
router.delete(
    "/instances/{instance_id}",
    response_model=OperationOut,
    status_code=202,
    tags=["instances"],
    description="Terminate: destroy the VM and release capacity. The record stays as TERMINATED.",
)(_action(OperationAction.TERMINATE))


@router.get("/instances/{instance_id}/events", response_model=EventPage, tags=["instances"])
def list_events(
    instance_id: str, service: Service, limit: Limit = 50, offset: Offset = 0
) -> EventPage:
    items, total = service.list_events(instance_id, limit, offset)
    return EventPage.model_validate(
        {"items": items, "total": total, "limit": limit, "offset": offset}, from_attributes=True
    )


@router.get(
    "/instances/{instance_id}/operations", response_model=OperationPage, tags=["operations"]
)
def list_operations(
    instance_id: str, service: Service, limit: Limit = 50, offset: Offset = 0
) -> OperationPage:
    items, total = service.list_operations(instance_id, limit, offset)
    return OperationPage.model_validate(
        {"items": items, "total": total, "limit": limit, "offset": offset}, from_attributes=True
    )


@router.get("/operations/{operation_id}", response_model=OperationOut, tags=["operations"])
def get_operation(operation_id: str, service: Service) -> OperationOut:
    return OperationOut.model_validate(service.get_operation(operation_id))


# ---- hosts --------------------------------------------------------------------------------


@router.get("/hosts", response_model=HostPage, tags=["hosts"])
def list_hosts(service: Service, limit: Limit = 50, offset: Offset = 0) -> HostPage:
    hosts = service.list_hosts().hosts
    page = hosts[offset : offset + limit]
    return HostPage(
        items=[HostOut.model_validate(h) for h in page],
        total=len(hosts),
        limit=limit,
        offset=offset,
    )


@router.get("/hosts/capacity", response_model=CapacityOut, tags=["hosts"])
def host_capacity(service: Service) -> CapacityOut:
    return CapacityOut.model_validate(service.capacity())


# ---- admin --------------------------------------------------------------------------------


@admin.post("/hosts/{name}/heartbeat", response_model=HostOut)
def host_heartbeat(name: str, body: HeartbeatRequest, service: Service) -> HostOut:
    """Called by the Go host agent. Replaces static capacity with the agent's measurement."""
    service.record_heartbeat(
        name, healthy=body.healthy, vcpus=body.vcpus, memory_mib=body.memory_mib
    )
    return HostOut.model_validate(next(h for h in service.list_hosts().hosts if h.name == name))


@admin.post("/hosts/{name}/status", response_model=HostOut)
def set_host_status(name: str, body: HostStatusRequest, service: Service) -> HostOut:
    service.set_host_status(name, HostStatus(body.status))
    return HostOut.model_validate(next(h for h in service.list_hosts().hosts if h.name == name))


@admin.get("/system")
def system_report() -> dict[str, object]:
    """Read-only OS/virtualization report for troubleshooting. Admin-only: it reveals host
    details. Runs no user-supplied command; the only subprocess is a fixed `sysctl` on macOS."""
    return sysinfo.doctor()


@admin.post("/reconcile")
def trigger_reconcile(
    reconciler: Annotated[Reconciler, Depends(get_reconciler)],
) -> dict[str, object]:
    return reconciler.run_once().as_dict()


@admin.post("/worker/drain")
def drain_worker(worker: Annotated[Worker, Depends(get_worker)]) -> dict[str, int]:
    """Run queued operations inline until the queue is empty (useful with the worker disabled)."""
    return {"executed": worker.run_until_idle()}


# ---- debug (failure injection, dev only) ------------------------------------------------------


@debug.post("/failures")
def inject_failure(
    body: FailureRequest,
    provider: Annotated[VMProvider, Depends(get_provider)],
    injector: Annotated[FailureInjector, Depends(get_injector)],
) -> dict[str, object]:
    if body.kind in {"provider_failure", "provider_timeout", "provider_delay"}:
        if not isinstance(provider, SimulatedProvider):
            return {"error": "provider-level injection requires the simulated provider"}
        if body.operation is None:
            return {"error": "operation is required for provider-level injection"}
        if body.kind == "provider_failure":
            provider.fail_next(body.operation, body.times, retryable=body.retryable)
        else:
            provider.set_latency(body.operation, body.delay_s)
    else:
        injector.arm(body.kind, body.times)
    return _failure_status(provider, injector)


@debug.get("/failures")
def failure_status(
    provider: Annotated[VMProvider, Depends(get_provider)],
    injector: Annotated[FailureInjector, Depends(get_injector)],
) -> dict[str, object]:
    return _failure_status(provider, injector)


def _failure_status(provider: VMProvider, injector: FailureInjector) -> dict[str, object]:
    status: dict[str, object] = {"control_plane": injector.status()}
    if isinstance(provider, SimulatedProvider):
        status["provider_failures"] = provider.pending_failures()
    return status


# ---- health / readiness / metrics ---------------------------------------------------------------


@public.get("/health")
def health(
    service: Service, provider: Annotated[VMProvider, Depends(get_provider)], response: Response
) -> dict[str, object]:
    """Liveness: the process is up and can reach its database."""
    try:
        service.ping_db()
        db = "ok"
    except Exception:  # noqa: BLE001
        db = "error"
        response.status_code = 503
    return {
        "status": "ok" if db == "ok" else "unavailable",
        "database": db,
        "provider": provider.describe(),
        "platform": platform.system(),
    }


@public.get("/ready")
def ready(
    service: Service,
    settings: SettingsDep,
    worker: Annotated[Worker, Depends(get_worker)],
    response: Response,
) -> dict[str, object]:
    """Readiness: safe to receive traffic (DB reachable, hosts seeded, worker thread alive)."""
    checks: dict[str, bool] = {}
    try:
        service.ping_db()
        checks["database"] = True
        checks["hosts_seeded"] = len(service.list_hosts().hosts) > 0
    except Exception:  # noqa: BLE001
        checks["database"] = False
        checks["hosts_seeded"] = False
    thread = getattr(worker, "thread", None)
    checks["worker"] = (not settings.worker_enabled) or bool(thread and thread.is_alive())
    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "checks": checks}


@public.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return render_all()
