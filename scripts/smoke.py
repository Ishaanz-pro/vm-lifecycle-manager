"""Standard-library-only smoke test: health, capacity, create, stop, start, terminate.

Deliberately has no third-party imports so it runs inside the production image (and inside
an offline network) with nothing extra installed.

  python scripts/smoke.py --base-url http://127.0.0.1:8000 [--api-key KEY] [--expect-no-egress]

--expect-no-egress first proves the environment really is disconnected by trying to reach
public addresses; the test FAILS if any connection succeeds.
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from typing import Any

EGRESS_PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("pypi.org", 443)]


class Client:
    def __init__(self, base: str, key: str | None) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("X-API-Key", self.key)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def prove_no_egress() -> None:
    for host, port in EGRESS_PROBES:
        try:
            with socket.create_connection((host, port), timeout=3):
                check(False, f"egress to {host}:{port} must be impossible, but it connected")
        except OSError as exc:
            check(True, f"no egress to {host}:{port} ({type(exc).__name__})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--expect-no-egress", action="store_true")
    args = ap.parse_args()
    c = Client(args.base_url, args.api_key)

    if args.expect_no_egress:
        prove_no_egress()

    status, health = c.call("GET", "/health")
    check(status == 200 and health["database"] == "ok", f"/health -> {status}")
    check(health["provider"]["simulated"] is True, "provider is reported as simulated")
    status, ready = c.call("GET", "/ready")
    check(status == 200, f"/ready -> {status} {ready.get('checks')}")

    _, cap0 = c.call("GET", "/v1/hosts/capacity")
    status, op = c.call(
        "POST", "/v1/instances?wait=15", {"name": "smoke", "vcpus": 1, "memory_mib": 256}
    )
    check(status == 200 and op["status"] == "SUCCEEDED", f"create -> {status} {op.get('status')}")
    iid = op["instance_id"]
    _, cap1 = c.call("GET", "/v1/hosts/capacity")
    check(cap1["reserved_vcpus"] == cap0["reserved_vcpus"] + 1, "capacity reserved on create")

    for action, final in (("stop", "STOPPED"), ("start", "RUNNING")):
        status, op = c.call("POST", f"/v1/instances/{iid}/{action}?wait=15")
        _, inst = c.call("GET", f"/v1/instances/{iid}")
        check(
            op["status"] == "SUCCEEDED" and inst["state"] == final, f"{action} -> {inst['state']}"
        )

    status, op = c.call("DELETE", f"/v1/instances/{iid}?wait=15")
    _, inst = c.call("GET", f"/v1/instances/{iid}")
    check(inst["state"] == "TERMINATED", f"terminate -> {inst['state']}")
    _, cap2 = c.call("GET", "/v1/hosts/capacity")
    check(cap2["reserved_vcpus"] == cap0["reserved_vcpus"], "capacity released on terminate")

    _, events = c.call("GET", f"/v1/instances/{iid}/events?limit=100")
    kinds = [e["event_type"] for e in events["items"]]
    check(kinds.count("resources_released") == 1, f"audit trail has {len(kinds)} events")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
