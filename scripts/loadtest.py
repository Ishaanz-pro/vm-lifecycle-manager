"""Closed-loop load test against a running control plane.

Each of C concurrent clients loops: create an instance (wait for completion), then terminate
it (wait for completion), until N full cycles have run in total. It reports throughput and
latency percentiles for the accept path (no wait) and the end-to-end path (with wait).

Assumptions (stated so results are not over-read):
  * simulated provider: provider calls take ~0 ms, so this measures the control plane
    (HTTP + SQLite transactions + worker loop), not virtualization
  * single uvicorn process, SQLite with BEGIN IMMEDIATE, so writes serialize by design
  * client and server on the same machine; numbers are machine-specific

Usage:
  python scripts/loadtest.py --base-url http://127.0.0.1:8000 --cycles 200 --concurrency 8
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--cycles", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    accept_ms: list[float] = []
    e2e_ms: list[float] = []
    errors: dict[str, int] = {}
    lock = threading.Lock()
    remaining = [args.cycles]

    def take() -> bool:
        with lock:
            if remaining[0] <= 0:
                return False
            remaining[0] -= 1
            return True

    def record(bucket: list[float], started: float) -> None:
        with lock:
            bucket.append((time.perf_counter() - started) * 1000)

    def fail(key: str) -> None:
        with lock:
            errors[key] = errors.get(key, 0) + 1

    def client_loop(worker: int) -> None:
        with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as c:
            i = 0
            while take():
                i += 1
                body = {"name": f"load-{worker}-{i}", "vcpus": 1, "memory_mib": 64}
                t0 = time.perf_counter()
                r = c.post("/v1/instances", json=body)
                record(accept_ms, t0)
                if r.status_code != 202:
                    fail(f"create {r.status_code}")
                    continue
                op = r.json()
                t1 = time.perf_counter()
                done = c.get(f"/v1/operations/{op['id']}")
                while done.json()["status"] in {"PENDING", "RUNNING"}:
                    time.sleep(0.005)
                    done = c.get(f"/v1/operations/{op['id']}")
                record(e2e_ms, t1)
                if done.json()["status"] != "SUCCEEDED":
                    fail("create failed")
                    continue
                t2 = time.perf_counter()
                r = c.delete(f"/v1/instances/{op['instance_id']}", params={"wait": 10})
                record(e2e_ms, t2)
                if r.status_code != 200 or r.json()["status"] != "SUCCEEDED":
                    fail(f"terminate {r.status_code}")

    started = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(client_loop, range(args.concurrency)))
    elapsed = time.perf_counter() - started

    ops = len(e2e_ms)
    print(f"cycles={args.cycles} concurrency={args.concurrency} elapsed={elapsed:.2f}s")
    print(f"lifecycle operations completed: {ops} ({ops / elapsed:.1f} ops/s)")
    for name, data in (("accept (POST, 202)", accept_ms), ("end-to-end (to SUCCEEDED)", e2e_ms)):
        print(
            f"{name:28s} n={len(data):5d} p50={percentile(data, 50):7.1f}ms "
            f"p95={percentile(data, 95):7.1f}ms p99={percentile(data, 99):7.1f}ms "
            f"mean={statistics.fmean(data) if data else float('nan'):7.1f}ms"
        )
    print(f"errors: {errors or 'none'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
