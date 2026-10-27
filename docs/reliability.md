# Reliability

What is guaranteed, how, and which test proves it. Nothing here claims distributed
consensus or exactly-once execution; neither is implemented.

## Guarantees

| Property | Mechanism | Tests |
|---|---|---|
| No capacity over-commit, per host | placement and insert in one `BEGIN IMMEDIATE` transaction (SQLite) / `SELECT ... FOR UPDATE` on host rows (PostgreSQL, untested) | `test_concurrency.py::test_concurrent_creates_never_overcommit_a_host`, `..._mixed_sizes_over_two_hosts...` |
| No double allocation / release | `reservations.instance_id` is the primary key; release deletes the row and reports whether it existed | `test_scheduler.py`, `test_api.py::test_terminate_releases_resources_exactly_once` |
| Accepted work is never lost | the `operations` row is written in the same transaction as the state change | `test_reconcile_and_restart.py::test_everything_survives_restart` |
| Each operation runs on one worker at a time | atomic claim + lease; outcome commit re-checks lease ownership | `test_concurrency.py::test_two_workers_execute_each_operation_exactly_once` |
| A stale worker cannot overwrite a newer outcome | outcome requires `claimed_by == me` and the instance `version` seen at claim | `test_worker.py::test_stale_outcome_from_a_dead_worker_is_dropped` |
| Conflicting concurrent requests: one wins | state-machine check inside the write transaction | `test_concurrency.py::test_concurrent_conflicting_actions_on_one_instance_admit_exactly_one` |
| Repeated client requests are safe | idempotency keys scoped per `(key, action:instance)` | `test_api.py` idempotency tests, `test_concurrency.py::test_concurrent_idempotent_creates_make_one_instance` |

**Execution semantics: at-least-once.** A provider call can run more than once (a timed-out
call keeps running in the background; a crashed worker's operation is re-run). This is
safe *only* because every provider call is idempotent for its control-plane-chosen
`provider_id`. That is a requirement on every provider implementation.

## Idempotency vs retry safety vs duplicate prevention

* **Idempotency**: sending the same request twice has the effect of sending it once.
  `Idempotency-Key` on create/start/stop/terminate returns the original operation (200).
  Same key with a different create body → `409 idempotency_conflict`. Keys expire after
  `VMLM_IDEMPOTENCY_TTL_S` (default 24 h); an expired key starts a new request. A request
  rejected before it was recorded (validation, capacity, invalid transition) does not
  consume its key.
* **Retry safety**: a failed or timed-out call can be repeated without harm. Provider calls
  are keyed by `provider_id`, so the worker can always retry.
* **Duplicate-operation prevention**: two different requests that conflict (two stops, or
  stop + terminate) cannot both be accepted: the first commits `STOPPING`, the second sees
  an illegal transition.

## Retries

* Transient errors (`ProviderError(retryable=True)`, `ProviderTimeout`) are retried up to
  `VMLM_MAX_ATTEMPTS` (default 3) with exponential backoff and **full jitter**:
  `delay = U(0, min(max_delay, base × 2^(attempt-1)))`.
* Permanent errors fail immediately.
* The operation returns to `PENDING` with `next_attempt_at` in the future, so a retry
  costs no worker thread while it waits, survives restarts, and may run on another worker.
* Capacity stays reserved across retries; it is released only by a final provisioning
  failure (rollback) or a successful terminate.
* Invariant enforced at startup: `worker_lease_s > provider_timeout_s`.

## Failure injection (development only)

Enabled only with `VMLM_FAILURE_INJECTION_ENABLED=true`; the endpoint additionally requires
the admin key when one is configured. `POST /v1/debug/failures`:

| Injected failure | How | Expected recovery | Test |
|---|---|---|---|
| Provisioning failure (permanent) | `{"kind":"provider_failure","operation":"provision"}` | op FAILED, instance FAILED, capacity released | `test_api.py::test_provisioning_failure_rolls_back_resources` |
| Transient provider failure | `... "retryable": true, "times": 2` | retried with backoff, then SUCCEEDED | `test_worker.py::test_transient_failure_is_retried_then_succeeds` |
| Attempts exhausted | `"times": 5` | FAILED after `max_attempts`, no extra calls | `test_worker.py::test_attempts_exhausted_fails_and_rolls_back` |
| Provider timeout / delayed response | `{"kind":"provider_delay","operation":"provision","delay_s":30}` | timeout → transient → retried; late call is harmless | `test_worker.py::test_provider_timeout_is_transient` |
| Worker crash after provider success | `{"kind":"worker_crash"}` | op stays RUNNING until lease expiry, then re-queued and re-run idempotently | `test_worker.py::test_worker_crash_leaves_lease_which_expires_and_requeues` |
| Database error on outcome commit | `{"kind":"db_error"}` | outcome rolled back atomically; lease expiry re-queues | `test_worker.py::test_database_error_on_outcome_commit_is_recovered_by_lease` |
| Crash, then restart | worker crash + new process | startup reconcile re-queues | `test_reconcile_and_restart.py::test_startup_requeues_operation_whose_worker_died` |
| Duplicate requests | repeat with/without `Idempotency-Key` | replay (200) / 409 | `test_api.py::test_duplicate_request_while_operation_in_flight_is_rejected` |
| Host capacity exhaustion | fill the host | `409 insufficient_capacity`, nothing persisted | `test_api.py::test_capacity_exceeded_rejected_without_side_effects` |
| External drift | `SimulatedProvider.set_external_state` | corrected, desired state restored | `test_reconcile_and_restart.py` drift tests |
| Unhealthy host | stop agent heartbeats | host UNHEALTHY, excluded from placement | `test_reconcile_and_restart.py::test_stale_heartbeat_marks_agent_host_unhealthy_and_recovers` |

Every test above drives time with an injected clock and the worker with `run_once()`:
there is no `sleep` and no timing-dependent assertion in the Python suite.

## Interrupted-work scenarios

| Scenario | Safe behaviour |
|---|---|
| VM should be RUNNING, provider says STOPPED | record STOPPED (`drift_detected`), queue `start` |
| Interrupted while provisioning | op stays RUNNING under lease → re-queued → `provision` re-runs idempotently |
| Worker restarted before recording completion | same: lease expiry, re-run, stale outcome rejected by version/lease check |
| Client repeats a request after a timeout | with the same `Idempotency-Key`: original operation returned; without one: `409` if it conflicts with the first |

## Limits (honest list)

* Single-node SQLite: one writer at a time; not safe across hosts or on network storage.
* Leases rely on wall-clock time on the database's host; large clock jumps can expire
  leases early (re-run, still safe) or late (slower recovery).
* The provider thread pool has 4 threads; persistent provider hangs can saturate it, after
  which further calls time out (transient) until the hung calls return.
* The reconciler runs inside the API process; with the API down, there is no drift
  correction (operations are still executed by a separate worker process, if running).
