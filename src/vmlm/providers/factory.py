"""Select the provider from configuration. Never falls back silently."""

from vmlm.config import Settings
from vmlm.providers.base import VMProvider
from vmlm.providers.simulated import SimulatedProvider


def build_provider(settings: Settings) -> VMProvider:
    if settings.provider == "simulated":
        return SimulatedProvider()
    if settings.provider == "qemu":
        from vmlm.providers.qemu import QemuProvider  # imported lazily: optional dependency

        return QemuProvider.from_settings(settings)  # raises ProviderUnavailable if unusable
    raise ValueError(f"unknown provider {settings.provider!r}")
