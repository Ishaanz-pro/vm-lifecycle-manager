"""Configuration validation: unsafe combinations must fail at startup, not at 3 a.m."""

import pytest
from pydantic import ValidationError

from vmlm.config import HostSpec, Settings


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"worker_lease_s": 5, "provider_timeout_s": 10}, "must exceed provider_timeout_s"),
        ({"worker_lease_s": 10, "provider_timeout_s": 10}, "must exceed provider_timeout_s"),
        ({"retry_base_delay_s": 5, "retry_max_delay_s": 1}, "retry_max_delay_s"),
        ({"hosts": []}, "at least one host"),
        (
            {"hosts": [HostSpec(name="a", vcpus=1, memory_mib=1)] * 2},
            "host names must be unique",
        ),
        ({"api_key": "same", "admin_api_key": "same"}, "must differ"),
        ({"hosts": [{"name": "bad name", "vcpus": 1, "memory_mib": 1}]}, "pattern"),
        ({"hosts": [{"name": "h", "vcpus": 0, "memory_mib": 1}]}, "greater than 0"),
        ({"provider": "vmware"}, "simulated"),
    ],
)
def test_unsafe_settings_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_hosts_parse_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "VMLM_HOSTS", '[{"name":"a","vcpus":4,"memory_mib":1024,"agent_managed":true}]'
    )
    s = Settings()
    assert s.hosts == [HostSpec(name="a", vcpus=4, memory_mib=1024, agent_managed=True)]


def test_secrets_do_not_leak_through_repr_or_dump() -> None:
    """Regression: keys were plain str, so repr(settings) printed them."""
    s = Settings(api_key="user-secret-value", admin_api_key="admin-secret-value")
    for rendered in (repr(s), str(s), str(s.model_dump()), s.model_dump_json()):
        assert "secret-value" not in rendered
    assert s.api_key is not None and s.api_key.get_secret_value() == "user-secret-value"
