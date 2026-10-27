"""Select the provider from configuration. Never falls back silently."""

from vmlm.config import Settings
from vmlm.providers.base import VMProvider
from vmlm.providers.simulated import SimulatedProvider


def build_provider(settings: Settings) -> VMProvider:
    if settings.provider == "simulated":
        provider = SimulatedProvider()
        if settings.simulated_latency_s > 0:
            for operation in ("provision", "start", "stop", "delete"):
                provider.set_latency(operation, settings.simulated_latency_s)
        return provider
    if settings.provider == "qemu":
        from vmlm.providers.qemu import QemuProvider  # imported lazily: optional dependency

        return QemuProvider.from_settings(settings)  # raises ProviderUnavailable if unusable
    raise ValueError(f"unknown provider {settings.provider!r}")
