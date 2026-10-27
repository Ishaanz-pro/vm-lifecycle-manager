"""Browser dashboard: static assets, security headers and the demo-mode key handoff."""

import pytest
from pydantic import ValidationError

from tests.conftest import MakeHarness
from vmlm.config import Settings

USER_KEY = "user-key-for-tests"
ADMIN_KEY = "admin-key-for-tests"
DEMO = {
    "api_key": USER_KEY,
    "admin_api_key": ADMIN_KEY,
    "demo_mode": True,
    "failure_injection_enabled": False,
}


def test_root_redirects_to_dashboard(make_harness: MakeHarness) -> None:
    h = make_harness()
    r = h.client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/ui/"


@pytest.mark.parametrize(
    "path,content_type",
    [("/ui/", "text/html"), ("/ui/app.js", "javascript"), ("/ui/style.css", "text/css")],
)
def test_assets_are_served_with_security_headers(
    make_harness: MakeHarness, path: str, content_type: str
) -> None:
    h = make_harness(api_key=USER_KEY, admin_api_key=ADMIN_KEY)
    r = h.client.get(path)  # static assets need no key: the page must load before auth
    assert r.status_code == 200
    assert content_type in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_dashboard_page_has_no_inline_script_or_third_party_requests(
    make_harness: MakeHarness,
) -> None:
    html = make_harness().client.get("/ui/").text
    assert '<script src="app.js">' in html
    assert "http://" not in html.replace("http://www.w3.org", "")
    for line in html.splitlines():
        if "<script" in line:
            assert "src=" in line, "inline script would violate the CSP"


def test_config_without_demo_mode_never_returns_a_key(make_harness: MakeHarness) -> None:
    h = make_harness(api_key=USER_KEY, admin_api_key=ADMIN_KEY)
    body = h.client.get("/ui/config").json()
    assert body == {"demo": False, "auth_required": True, "api_key": None}
    assert USER_KEY not in str(body) and ADMIN_KEY not in str(body)


def test_config_with_auth_disabled(make_harness: MakeHarness) -> None:
    body = make_harness().client.get("/ui/config").json()
    assert body == {"demo": False, "auth_required": False, "api_key": None}


def test_demo_mode_hands_out_only_the_user_key(make_harness: MakeHarness) -> None:
    h = make_harness(**DEMO)
    r = h.client.get("/ui/config")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["demo"] is True and body["api_key"] == USER_KEY
    assert ADMIN_KEY not in r.text

    # The handed-out key works for the lifecycle API ...
    user = {"X-API-Key": body["api_key"]}
    assert h.client.get("/v1/hosts/capacity", headers=user).status_code == 200
    # ... but unlocks nothing administrative or debug-related.
    assert h.client.post("/v1/admin/reconcile", headers=user).status_code == 403
    assert h.client.get("/v1/admin/system", headers=user).status_code == 403
    assert h.client.get("/v1/debug/failures", headers=user).status_code in (401, 403)


def test_dashboard_calls_the_real_api_end_to_end(make_harness: MakeHarness) -> None:
    """What the page does: create with an Idempotency-Key, poll, replay, terminate."""
    h = make_harness(worker_enabled=False, **DEMO)
    user = {"X-API-Key": USER_KEY, "Idempotency-Key": "ui-key-1"}
    body = {"name": "ui-vm", "vcpus": 1, "memory_mib": 1024}

    first = h.client.post("/v1/instances", json=body, headers=user)
    assert first.status_code == 202
    replay = h.client.post("/v1/instances", json=body, headers=user)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]

    h.drain()
    listing = h.client.get("/v1/instances?limit=100", headers=user).json()
    assert [i["state"] for i in listing["items"]] == ["RUNNING"]


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"demo_mode": True}, "requires both api_key and admin_api_key"),
        ({"demo_mode": True, "api_key": "a"}, "requires both api_key and admin_api_key"),
        (
            {
                "demo_mode": True,
                "api_key": "a",
                "admin_api_key": "b",
                "failure_injection_enabled": True,
            },
            "failure injection",
        ),
        ({"simulated_latency_s": 5, "provider_timeout_s": 3}, "below provider_timeout_s"),
        ({"simulated_latency_s": 10}, "less than or equal to 5"),
    ],
)
def test_demo_settings_are_validated(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_simulated_latency_is_applied_to_the_provider() -> None:
    from vmlm.providers.factory import build_provider

    provider = build_provider(Settings(simulated_latency_s=0.25))
    assert provider._latency == {  # type: ignore[attr-defined]
        "provision": 0.25,
        "start": 0.25,
        "stop": 0.25,
        "delete": 0.25,
    }
    assert build_provider(Settings())._latency == {}  # type: ignore[attr-defined]
