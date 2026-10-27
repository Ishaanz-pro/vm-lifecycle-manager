"""Minimal in-process metrics registry rendered in the Prometheus text exposition format.

Deliberately dependency-free (~100 lines) to show the mechanics. Label values must come
from a bounded set (actions, outcomes, host names from config, route templates); never put
instance IDs, request IDs or user input into labels, which would create unbounded series.

Metric catalogue (names, types, units, labels) is in docs/architecture.md#metrics.
"""

import threading
from bisect import bisect_right
from collections.abc import Iterator

_LOCK = threading.Lock()
_REGISTRY: list["_Metric"] = []

Labels = tuple[tuple[str, str], ...]


def _labels(kw: dict[str, str]) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in kw.items()))


def _fmt(labels: Labels, extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in labels]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_: str) -> None:
        self.name, self.help = name, help_
        with _LOCK:
            _REGISTRY.append(self)

    def render(self) -> Iterator[str]:
        raise NotImplementedError


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_: str) -> None:
        super().__init__(name, help_)
        self._values: dict[Labels, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labels(labels)
        with _LOCK:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with _LOCK:
            return self._values.get(_labels(labels), 0.0)

    def render(self) -> Iterator[str]:
        with _LOCK:
            items = sorted(self._values.items())
        for labels, v in items:
            yield f"{self.name}{_fmt(labels)} {v:g}"


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, name: str, help_: str) -> None:
        super().__init__(name, help_)
        self._values: dict[Labels, float] = {}

    def set(self, value: float, **labels: str) -> None:
        with _LOCK:
            self._values[_labels(labels)] = value

    def value(self, **labels: str) -> float:
        with _LOCK:
            return self._values.get(_labels(labels), 0.0)

    def render(self) -> Iterator[str]:
        with _LOCK:
            items = sorted(self._values.items())
        for labels, v in items:
            yield f"{self.name}{_fmt(labels)} {v:g}"


class Histogram(_Metric):
    kind = "histogram"
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

    def __init__(self, name: str, help_: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        super().__init__(name, help_)
        self._buckets = buckets
        self._counts: dict[Labels, list[int]] = {}
        self._sum: dict[Labels, float] = {}
        self._n: dict[Labels, int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = _labels(labels)
        with _LOCK:
            counts = self._counts.setdefault(key, [0] * (len(self._buckets) + 1))
            counts[bisect_right(self._buckets, value)] += 1
            self._sum[key] = self._sum.get(key, 0.0) + value
            self._n[key] = self._n.get(key, 0) + 1

    def count(self, **labels: str) -> int:
        with _LOCK:
            return self._n.get(_labels(labels), 0)

    def render(self) -> Iterator[str]:
        with _LOCK:
            items = sorted(self._counts.items())
            sums, ns = dict(self._sum), dict(self._n)
        for labels, counts in items:
            cumulative = 0
            for bound, c in zip(self._buckets, counts, strict=False):
                cumulative += c
                le = 'le="' + f"{bound:g}" + '"'
                yield f"{self.name}_bucket{_fmt(labels, le)} {cumulative}"
            inf = 'le="+Inf"'
            yield f"{self.name}_bucket{_fmt(labels, inf)} {ns[labels]}"
            yield f"{self.name}_sum{_fmt(labels)} {sums[labels]:g}"
            yield f"{self.name}_count{_fmt(labels)} {ns[labels]}"


def render_all() -> str:
    with _LOCK:
        metrics = list(_REGISTRY)
    lines: list[str] = []
    for m in metrics:
        lines.append(f"# HELP {m.name} {m.help}")
        lines.append(f"# TYPE {m.name} {m.kind}")
        lines.extend(m.render())
    return "\n".join(lines) + "\n"


# ---- catalogue ---------------------------------------------------------------------------

HTTP_REQUESTS = Counter("vmlm_http_requests_total", "HTTP requests by route template and status")
HTTP_LATENCY = Histogram("vmlm_http_request_duration_seconds", "HTTP request latency")
OPERATIONS = Counter("vmlm_operations_total", "Completed lifecycle operations by action/outcome")
OPERATION_DURATION = Histogram(
    "vmlm_operation_duration_seconds", "Wall time from operation creation to completion"
)
OPERATION_ATTEMPTS = Counter("vmlm_operation_attempts_total", "Worker attempts by action")
PROVIDER_FAILURES = Counter(
    "vmlm_provider_failures_total", "Provider call failures by operation and kind"
)
SCHEDULER_REJECTIONS = Counter("vmlm_scheduler_rejections_total", "Placements rejected by reason")
HOST_CAPACITY = Gauge("vmlm_host_capacity", "Host capacity by host, resource and kind")
OPERATIONS_QUEUED = Gauge("vmlm_operations_pending", "Operations waiting for a worker")
RECONCILE_REPAIRS = Counter("vmlm_reconcile_repairs_total", "Reconciler repairs by kind")
