"""Request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vmlm.domain.states import VMState


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
    provider_id: str
    failure_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: str
    event_type: str
    from_state: str | None
    to_state: str | None
    detail: str | None
    created_at: datetime


class InstancePage(BaseModel):
    items: list[InstanceOut]
    total: int
    limit: int
    offset: int


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class CapacityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_vcpus: int
    reserved_vcpus: int
    available_vcpus: int
    total_memory_mib: int
    reserved_memory_mib: int
    available_memory_mib: int
    active_reservations: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody
