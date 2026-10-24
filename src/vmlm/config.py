"""Runtime settings, read from VMLM_* environment variables. No secrets are hardcoded."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMLM_", extra="ignore")

    database_url: str = "sqlite:///./vmlm.db"
    host_vcpus: int = Field(default=16, gt=0)
    host_memory_mib: int = Field(default=65536, gt=0)
    api_key: str | None = None  # when unset, authentication is disabled
    log_level: str = "INFO"
