# Design decisions and trade-offs

## Durable operation queue in the database, not a broker

Operations are rows written in the same transaction as the state change they belong to.
There is no window where a request is acknowledged but its work is not recorded, and no
second system to keep consistent. Cost: the database is also the queue, so queue throughput
is bounded by database write throughput. A broker would be the next step only once that
bound is the measured bottleneck.

## Leases instead of locks for in-flight work

A worker never holds a database transaction during a provider call (calls can take
seconds or minutes). It holds a *lease* recorded in the row. Recovery from a dead worker is
"wait for the lease to expire", not "detect the crash", which needs no failure detector. The
price is at-least-once execution, made safe by idempotent provider calls.

## SQLite with an explicit read/write transaction split

Writes use `BEGIN IMMEDIATE` (take the single write lock up front, so check-then-act
sequences cannot interleave). Reads use a deferred `BEGIN` under WAL and never block or wait
for the writer.

This split was introduced *because of a measurement*. Load test, 200 create+terminate
cycles, simulated provider, single uvicorn process, Windows 11 laptop (8 logical CPUs),
client on the same machine (`scripts/loadtest.py`):

| Build | Concurrency | Throughput | End-to-end p50 | p95 | p99 | Notes |
|---|---|---|---|---|---|---|
| all transactions `BEGIN IMMEDIATE` | 1 | 29.0 ops/s | 28.6 ms | 42.7 ms | 55.2 ms | |
| all transactions `BEGIN IMMEDIATE` | 8 | **18.2 ops/s** | 171.5 ms | **1442 ms** | **2744 ms** | more clients, *less* throughput: polling reads queued on the write lock |
| read/write split | 1 | 28.3 ops/s | 29.3 ms | 54.0 ms | 57.0 ms | no change, as expected |
| read/write split (3 runs) | 8 | **33.6–34.3 ops/s** | 106–132 ms | **273–359 ms** | 705–1294 ms | |

One run per row unless noted; machine-specific; not a capacity claim for any other setup.

Unresolved: accept-path (`POST`) p99 at concurrency 8 is 1.4–1.8 s in every run, because all
writers (requests, worker claims, outcome commits) share one lock.

**A tried-and-rejected change:** having the idle worker "peek" with a read transaction
before taking the write lock to claim. Hypothesis: idle polling (every 10 ms) was competing
with request writes. Measured over 3 runs at concurrency 8: 29.7–32.5 ops/s (slightly *worse*)
and no improvement in accept p99. Under load the queue is rarely empty, so the peek only
added a transaction. Reverted.

## Best-fit placement

Packs instances tightly so large requests still find a host. Alternative "least allocated"
spreads load and reduces the blast radius of one host failing, at the cost of
fragmentation. For a capacity-constrained single-rack simulation, best fit was chosen;
the policy is one `min()` key in `Scheduler.place`, O(R + H) per placement.

## Reserve at accept time

Capacity is reserved when the request is accepted, not when provisioning completes. A
client gets a definitive yes/no immediately and two accepted requests can never both find
the last slot gone. A failed provisioning releases the reservation in the same transaction
that marks it FAILED.

## Never re-create a missing VM automatically

If a RUNNING instance's VM disappears, the reconciler marks it FAILED and keeps the
capacity. Re-creating it automatically could duplicate a workload that still exists
somewhere the control plane cannot see (e.g. a partitioned host). An operator decides.

## Python control plane, Go host agent

The agent runs on every host, continuously, alongside the workloads: a single static
binary with no runtime, a small and predictable memory footprint, cheap concurrency, and
first-class context cancellation and deadlines are exactly what Go is good at, and its
container image is distroless. The control plane is request/response business logic over a
database, where Python + FastAPI + SQLAlchemy give the clearest code and the existing,
tested implementation already lives. Rewriting it in Go would add risk and no capability.
The two meet at one small, versioned HTTP endpoint (`POST /v1/admin/hosts/{name}/heartbeat`),
and the control plane works fully without the agent (static hosts).

## Dependency-free metrics

A ~150-line Prometheus-text registry instead of `prometheus_client`: one less dependency to
pin for the air-gapped bundle, and the mechanics (counters, cumulative histogram buckets,
bounded label sets) are visible. A real deployment would use the standard client library
and OpenTelemetry.

## Single replica on Kubernetes

SQLite on a ReadWriteOnce volume supports one writer. The Deployment uses `replicas: 1` and
`strategy: Recreate`. Horizontal scaling requires PostgreSQL first; the scheduler already
issues `SELECT ... FOR UPDATE` and the worker `SKIP LOCKED` for that case, but that path is
not covered by the test suite.
