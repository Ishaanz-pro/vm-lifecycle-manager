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
leaves a visible marker that reconciliation can resolve.
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


def assert_transition(current: VMState, target: VMState, action: str | None = None) -> None:
    if target not in TRANSITIONS[current]:
        what = f"cannot {action} an instance" if action else f"cannot move {current} to {target}"
        raise InvalidTransition(
            f"{what} in state {current}",
            {"current_state": current.value, "target_state": target.value},
        )
