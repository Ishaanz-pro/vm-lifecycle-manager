"""Thin HTTP handlers: parse input, call the service, shape output."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from vmlm.api.deps import get_service, require_api_key
from vmlm.api.schemas import (
    CapacityOut,
    CreateInstanceRequest,
    ErrorResponse,
    EventPage,
    InstanceOut,
    InstancePage,
)
from vmlm.domain.service import LifecycleService
from vmlm.domain.states import VMState

Service = Annotated[LifecycleService, Depends(get_service)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)], responses=ERRORS)
public_router = APIRouter()


@router.post("/instances", response_model=InstanceOut, status_code=201)
def create_instance(
    body: CreateInstanceRequest,
    response: Response,
    service: Service,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ] = None,
) -> InstanceOut:
    inst, created = service.create_instance(body.name, body.vcpus, body.memory_mib, idempotency_key)
    response.status_code = 201 if created else 200  # 200 = replay of an earlier request
    response.headers["Location"] = f"/v1/instances/{inst.id}"
    return InstanceOut.model_validate(inst)


@router.get("/instances", response_model=InstancePage)
def list_instances(
    service: Service,
    state: VMState | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> InstancePage:
    items, total = service.list_instances(state, limit, offset)
    return InstancePage(
        items=[InstanceOut.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/instances/{instance_id}", response_model=InstanceOut)
def get_instance(instance_id: str, service: Service) -> InstanceOut:
    return InstanceOut.model_validate(service.get_instance(instance_id))


@router.post("/instances/{instance_id}/start", response_model=InstanceOut)
def start_instance(instance_id: str, service: Service) -> InstanceOut:
    return InstanceOut.model_validate(service.start_instance(instance_id))


@router.post("/instances/{instance_id}/stop", response_model=InstanceOut)
def stop_instance(instance_id: str, service: Service) -> InstanceOut:
    return InstanceOut.model_validate(service.stop_instance(instance_id))


@router.delete("/instances/{instance_id}", response_model=InstanceOut)
def terminate_instance(instance_id: str, service: Service) -> InstanceOut:
    """Terminate: destroys the VM, releases resources; the record stays as TERMINATED."""
    return InstanceOut.model_validate(service.terminate_instance(instance_id))


@router.get("/instances/{instance_id}/events", response_model=EventPage)
def list_events(
    instance_id: str, service: Service, limit: Limit = 50, offset: Offset = 0
) -> EventPage:
    items, total = service.list_events(instance_id, limit, offset)
    return EventPage.model_validate(
        {"items": items, "total": total, "limit": limit, "offset": offset},
        from_attributes=True,
    )


@router.get("/hosts/capacity", response_model=CapacityOut)
def host_capacity(service: Service) -> CapacityOut:
    return CapacityOut.model_validate(service.capacity())


@public_router.get("/health")
def health(service: Service, response: Response) -> dict[str, str]:
    try:
        service.ping_db()
    except Exception:
        response.status_code = 503
        return {"status": "unavailable", "database": "error"}
    return {"status": "ok", "database": "ok"}
