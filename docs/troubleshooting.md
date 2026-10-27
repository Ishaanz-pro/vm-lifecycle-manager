# Troubleshooting

Start with three facts: the **request ID** (every response has `X-Request-ID`, and every
error body includes it), the **operation** (`GET /v1/operations/{id}`), and the **audit
trail** (`GET /v1/instances/{id}/events`). Log lines carry the same `request_id` and
`operation_id`, so `grep <request_id>` follows one request from the API into the worker.

```bash
python -m vmlm.sysinfo            # OS, CPUs, memory, paths, virtualization capabilities
curl -s localhost:8000/ready      # which readiness check is failing
curl -s localhost:8000/metrics | grep -E 'provider_failures|scheduler_rejections|pending'
curl -s -H "X-API-Key: $ADMIN" localhost:8000/v1/admin/system   # same report, over HTTP
```

## An operation stays PENDING

1. `curl localhost:8000/ready` → `checks.worker` false? The worker thread died or is disabled
   (`VMLM_WORKER_ENABLED=false` with no `vmlm.worker_main` process running).
2. `next_attempt_at` in the future? It is waiting out a retry backoff; `error` says why.
3. Workaround with the worker disabled: `POST /v1/admin/worker/drain` runs the queue inline.

## An operation stays RUNNING

A worker claimed it and has not reported back. If the worker process died, the lease
expires after `VMLM_WORKER_LEASE_S` and the next reconcile pass re-queues it
(`operation_requeued` in the audit trail). Force a pass: `POST /v1/admin/reconcile`.

Reproduce (dev only):

```bash
export VMLM_FAILURE_INJECTION_ENABLED=true VMLM_WORKER_LEASE_S=15 VMLM_PROVIDER_TIMEOUT_S=5
curl -X POST localhost:8000/v1/debug/failures -d '{"kind":"worker_crash"}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/v1/instances -d '{"name":"x","vcpus":1,"memory_mib":128}' -H 'Content-Type: application/json'
# op is RUNNING; ~15 s later the reconciler re-queues it and it SUCCEEDS on attempt 2
```

## `409 insufficient_capacity`

`details.reason`:
* `insufficient_capacity`: no READY host has room on *both* dimensions; `details` shows the
  largest free vCPU and MiB on any single host. Fragmentation is possible: 4 + 4 free
  vCPUs on two hosts cannot fit one 8-vCPU instance.
* `no_eligible_hosts`: every host is DRAINING or UNHEALTHY. Check `GET /v1/hosts`.

## A host is UNHEALTHY

Agent-managed hosts go UNHEALTHY when no heartbeat arrives for `VMLM_HEARTBEAT_STALE_S`.
Check the agent: `curl localhost:9100/readyz` and `/v1/status` (`last_error`,
`consecutive_failures`). Common causes: wrong `VMLM_AGENT_CONTROL_PLANE_URL`, wrong/missing
`VMLM_AGENT_API_KEY` (control plane returns 401/403; visible in `last_error`), host name
not present in `VMLM_HOSTS` (404).

## `database is locked`

SQLite allows one writer. Writers wait up to 30 s (`busy_timeout`). Persistent lock errors
mean something holds a write transaction for a long time: check for a second process on the
same file over a network filesystem (unsupported) or an external tool with the DB open.

## QEMU provider will not start

The startup error lists every missing prerequisite. Run `python -m vmlm.sysinfo` and check
`virtualization.accelerators` and `virtualization.qemu`. On macOS `kvm` is never
available; use `VMLM_QEMU_ACCEL=hvf`. For a VM that dies immediately, read
`qemu-state/<provider_id>/console.log`.

## Port already in use

```bash
python -c "from vmlm import sysinfo; print(sysinfo.check_tcp_port('127.0.0.1', 8000))"
lsof -iTCP:8000 -sTCP:LISTEN     # macOS/Linux: which process holds it
```

## Exit codes and signals

`sysinfo.run_command` reports `exit_code` and, on POSIX, the terminating `signal` (a child
killed by SIGTERM has return code `-15`). On timeout the whole process group gets SIGTERM,
then SIGKILL after a grace period, so no grandchild is left orphaned (tested).
