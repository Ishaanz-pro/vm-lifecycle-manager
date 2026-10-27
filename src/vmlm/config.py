"""Runtime settings, read from VMLM_* environment variables. No secrets are hardcoded.

Secrets (API keys) are only ever read from the environment and are never logged.
"""

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HostSpec(BaseModel):
    """Static description of a schedulable host. Set VMLM_HOSTS to a JSON list of these."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    vcpus: int = Field(gt=0, le=4096)
    memory_mib: int = Field(gt=0, le=16_777_216)
    agent_managed: bool = False  # True: health comes from the Go host agent's heartbeats


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMLM_", extra="ignore")

    database_url: str = "sqlite:///./vmlm.db"
    hosts: list[HostSpec] = [HostSpec(name="host-1", vcpus=16, memory_mib=65536)]
    provider: Literal["simulated", "qemu"] = "simulated"

    # Authentication. Unset = disabled. The admin key additionally unlocks /v1/admin/*.
    # SecretStr: repr/str/model_dump print "**********", so an accidental log of the settings
    # object cannot leak a key. Read the value only where it is compared (api/deps.py).
    api_key: SecretStr | None = None
    admin_api_key: SecretStr | None = None

    # Worker / operations
    worker_enabled: bool = True
    worker_poll_interval_s: float = Field(default=0.2, gt=0)
    worker_lease_s: float = Field(default=30.0, gt=0)
    provider_timeout_s: float = Field(default=10.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    retry_base_delay_s: float = Field(default=0.5, ge=0)
    retry_max_delay_s: float = Field(default=10.0, ge=0)
    shutdown_grace_s: float = Field(default=10.0, ge=0)

    # Reconciliation
    reconcile_interval_s: float = Field(default=30.0, ge=0)  # 0 disables the periodic loop
    heartbeat_stale_s: float = Field(default=60.0, gt=0)

    idempotency_ttl_s: int = Field(default=86_400, gt=0)

    # Development-only controls. Never enable in a shared deployment.
    failure_injection_enabled: bool = False

    # QEMU provider (only read when provider == "qemu")
    qemu_binary: str = "qemu-system-aarch64"
    qemu_img_binary: str = "qemu-img"
    qemu_base_image: str | None = None
    qemu_state_dir: str = "./qemu-state"
    qemu_accel: Literal["hvf", "kvm", "tcg"] = "hvf"
    qemu_machine: str = "virt"
    qemu_firmware: str | None = None  # e.g. edk2-aarch64-code.fd for aarch64 "virt"
    qemu_shutdown_timeout_s: float = Field(default=20.0, gt=0)

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_invariants(self) -> "Settings":
        # A live worker must always be able to report before its lease runs out; otherwise a
        # slow-but-healthy call gets its operation re-run by another worker.
        if self.worker_lease_s <= self.provider_timeout_s:
            raise ValueError(
                f"worker_lease_s ({self.worker_lease_s}) must exceed provider_timeout_s "
                f"({self.provider_timeout_s})"
            )
        if self.retry_max_delay_s < self.retry_base_delay_s:
            raise ValueError("retry_max_delay_s must be >= retry_base_delay_s")
        names = [h.name for h in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("host names must be unique")
        if not self.hosts:
            raise ValueError("at least one host must be configured")
        if (
            self.api_key
            and self.admin_api_key
            and self.api_key.get_secret_value() == self.admin_api_key.get_secret_value()
        ):
            raise ValueError("api_key and admin_api_key must differ")
        return self
