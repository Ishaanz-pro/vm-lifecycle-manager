"""Structured logs, correlation IDs and the metrics registry."""

import json
import logging

import pytest

from tests.conftest import BODY, Harness, MakeHarness
from vmlm.logging_config import JsonFormatter
from vmlm.observability import metrics as m


def test_metrics_registry_renders_prometheus_text() -> None:
    c = m.Counter("test_counter_total", "a counter")
    g = m.Gauge("test_gauge", "a gauge")
    hist = m.Histogram("test_hist_seconds", "a histogram", buckets=(0.1, 1.0))
    c.inc(kind="x")
    c.inc(2, kind="x")
    g.set(3.5, host="h1")
    hist.observe(0.05)
    hist.observe(0.5)
    hist.observe(5)
    text = m.render_all()
    assert "# TYPE test_counter_total counter" in text
    assert 'test_counter_total{kind="x"} 3' in text
    assert 'test_gauge{host="h1"} 3.5' in text
    assert 'test_hist_seconds_bucket{le="0.1"} 1' in text
    assert 'test_hist_seconds_bucket{le="1"} 2' in text
    assert 'test_hist_seconds_bucket{le="+Inf"} 3' in text
    assert "test_hist_seconds_sum 5.55" in text and "test_hist_seconds_count 3" in text


def test_http_metrics_use_route_templates_not_ids(h: Harness) -> None:
    inst = h.create()
    before = m.HTTP_REQUESTS.value(method="GET", route="/v1/instances/{instance_id}", status="200")
    h.get(f"/v1/instances/{inst['id']}")
    h.get("/v1/instances/missing")
    assert (
        m.HTTP_REQUESTS.value(method="GET", route="/v1/instances/{instance_id}", status="200")
        == before + 1
    )
    assert (
        m.HTTP_REQUESTS.value(method="GET", route="/v1/instances/{instance_id}", status="404") >= 1
    )
    text = h.get("/metrics").text
    assert inst["id"] not in text  # no unbounded-cardinality labels
    assert 'route="/v1/instances/{instance_id}"' in text


def test_operation_and_provider_metrics(h: Harness) -> None:
    ok_before = m.OPERATIONS.value(action="create", outcome="succeeded")
    fail_before = m.PROVIDER_FAILURES.value(operation="create", kind="permanent")
    rej_before = m.SCHEDULER_REJECTIONS.value(reason="insufficient_capacity")
    h.create()
    h.provider.fail_next("provision")
    h.create()
    h.post("/v1/instances", json={**BODY, "vcpus": 1000})
    assert m.OPERATIONS.value(action="create", outcome="succeeded") == ok_before + 1
    assert m.OPERATIONS.value(action="create", outcome="failed") >= 1
    assert m.PROVIDER_FAILURES.value(operation="create", kind="permanent") == fail_before + 1
    assert m.SCHEDULER_REJECTIONS.value(reason="insufficient_capacity") == rej_before + 1
    assert m.OPERATION_DURATION.count(action="create") >= 2
    h.reconciler.run_once()
    assert m.HOST_CAPACITY.value(host="host-1", resource="vcpus", kind="reserved") == 2.0


def test_logs_are_json_with_correlation_ids(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vmlm")
    resp = h.post("/v1/instances", json=BODY, headers={"X-Request-ID": "req-42"})
    op_id = resp.json()["id"]
    h.drain()
    fmt = JsonFormatter()
    lines = [json.loads(fmt.format(r)) for r in caplog.records if r.name.startswith("vmlm")]
    accepted = next(line for line in lines if line["event"] == "instance_create_accepted")
    assert accepted["request_id"] == "req-42" and accepted["level"] == "INFO" and "ts" in accepted
    changed = next(line for line in lines if line["event"] == "state_changed")
    assert (
        changed["request_id"] == "req-42" and changed["operation_id"] == op_id
    )  # carried into the worker


def test_unhandled_errors_do_not_leak_details(
    make_harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(raise_server_exceptions=False)  # a real server converts it to a 500

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret internal path /etc/vmlm")

    monkeypatch.setattr(h.service, "capacity", boom)
    resp = h.get("/v1/hosts/capacity")
    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["code"] == "internal_error" and "secret" not in resp.text and "request_id" in body
