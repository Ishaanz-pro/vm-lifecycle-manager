"""Explicit VM state machine.

    PROVISIONING -> RUNNING | FAILED
    RUNNING      -> STOPPING | TERMINATING
    STOPPING     -> STOPPED | FAILED
    STOPPED      -> STARTING | TERMINATING
    STARTING     -> RUNNING | FAILED
    FAILED       -> TERMINATING            (only cleanup is possible)
    TERMINATING  -> TERMINATED | FAILED    (FAILED = provider delete failed; retry via DELETE)
    TERMINATED   -> (terminal)

The -ING states are committed BEFORE the provider is called, so a crash mid-operation
leaves a visible marker that the worker lease / reconciler can resolve.

Besides the observed `state`, every instance carries a `desired_state` (what the user last
asked for). The reconciler compares desired state with what the provider reports.
"""

from enum import StrEnum

from vmlm.domain.errors import InvalidTransition


class VMState(StrEnum):
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"
    FAILED = "FAILED"


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    TERMINATED = "TERMINATED"


class OperationAction(StrEnum):
    CREATE = "create"
    START = "start"
    STOP = "stop"
    TERMINATE = "terminate"


class OperationStatus(StrEnum):
    PENDING = "PENDING"  # queued, or waiting for its retry time
    RUNNING = "RUNNING"  # claimed by a worker under a lease
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


S = VMState
TRANSITIONS: dict[VMState, frozenset[VMState]] = {
    S.PROVISIONING: frozenset({S.RUNNING, S.FAILED}),
    S.RUNNING: frozenset({S.STOPPING, S.TERMINATING}),
    S.STOPPING: frozenset({S.STOPPED, S.FAILED}),
    S.STOPPED: frozenset({S.STARTING, S.TERMINATING}),
    S.STARTING: frozenset({S.RUNNING, S.FAILED}),
    S.FAILED: frozenset({S.TERMINATING}),
    S.TERMINATING: frozenset({S.TERMINATED, S.FAILED}),
    S.TERMINATED: frozenset(),
}

TRANSITIONAL_STATES = frozenset({S.PROVISIONING, S.STOPPING, S.STARTING, S.TERMINATING})
TERMINAL_OPERATION_STATUSES = frozenset({OperationStatus.SUCCEEDED, OperationStatus.FAILED})

# action -> (transitional state committed before the provider call, final state on success)
ACTION_STATES: dict[OperationAction, tuple[VMState, VMState]] = {
    OperationAction.CREATE: (S.PROVISIONING, S.RUNNING),
    OperationAction.START: (S.STARTING, S.RUNNING),
    OperationAction.STOP: (S.STOPPING, S.STOPPED),
    OperationAction.TERMINATE: (S.TERMINATING, S.TERMINATED),
}

ACTION_DESIRED: dict[OperationAction, DesiredState] = {
    OperationAction.CREATE: DesiredState.RUNNING,
    OperationAction.START: DesiredState.RUNNING,
    OperationAction.STOP: DesiredState.STOPPED,
    OperationAction.TERMINATE: DesiredState.TERMINATED,
}


def assert_transition(current: VMState, target: VMState, action: str | None = None) -> None:
    if target not in TRANSITIONS[current]:
        what = f"cannot {action} an instance" if action else f"cannot move {current} to {target}"
        raise InvalidTransition(
            f"{what} in state {current}",
            {"current_state": current.value, "target_state": target.value},
        )
