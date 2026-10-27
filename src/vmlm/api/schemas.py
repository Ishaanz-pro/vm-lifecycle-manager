"""Request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vmlm.db.models import HostStatus
from vmlm.domain.states import DesiredState, OperationAction, OperationStatus, VMState


class CreateInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    vcpus: int = Field(gt=0, le=1024, strict=True)
    memory_mib: int = Field(gt=0, le=4_194_304, strict=True)


class InstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    vcpus: int
    memory_mib: int
    state: VMState
    desired_state: DesiredState
    host_id: str | None
    provider_id: str
    failure_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    instance_id: str
    action: OperationAction
    status: OperationStatus
    from_state: VMState
    target_state: VMState
    attempts: int
    max_attempts: int
    error: str | None
    error_kind: str | None
    request_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    next_attempt_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: str
    operation_id: str | None
    event_type: str
    from_state: str | None
    to_state: str | None
    detail: str | None
    request_id: str | None
    created_at: datetime


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class CapacityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_vcpus: int
    reserved_vcpus: int
    available_vcpus: int
    total_memory_mib: int
    reserved_memory_mib: int
    available_memory_mib: int
    active_reservations: int
    hosts: list[HostOut]


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class InstancePage(Page):
    items: list[InstanceOut]


class OperationPage(Page):
    items: list[OperationOut]


class EventPage(Page):
    items: list[EventOut]


class HostPage(Page):
    items: list[HostOut]


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool
    vcpus: int | None = Field(default=None, gt=0, le=4096)
    memory_mib: int | None = Field(default=None, gt=0, le=16_777_216)
    agent_version: str | None = Field(default=None, max_length=32)


class HostStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["READY", "DRAINING"]


class FailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "provider_failure", "provider_timeout", "provider_delay", "worker_crash", "db_error"
    ]
    operation: Literal["provision", "start", "stop", "delete", "inspect"] | None = None
    times: int = Field(default=1, ge=1, le=100)
    retryable: bool = False
    delay_s: float = Field(default=0.0, ge=0, le=60)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody
