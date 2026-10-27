import itertools

import pytest

from vmlm.domain.errors import InvalidTransition
from vmlm.domain.reconciler import OBSERVED_CORRECTIONS, RESTORE_ACTION
from vmlm.domain.states import (
    ACTION_DESIRED,
    ACTION_STATES,
    TRANSITIONS,
    DesiredState,
    OperationAction,
    VMState,
    assert_transition,
)
from vmlm.providers.base import ProviderVMState

LEGAL = {
    (VMState.PROVISIONING, VMState.RUNNING),
    (VMState.PROVISIONING, VMState.FAILED),
    (VMState.RUNNING, VMState.STOPPING),
    (VMState.RUNNING, VMState.TERMINATING),
    (VMState.STOPPING, VMState.STOPPED),
    (VMState.STOPPING, VMState.FAILED),
    (VMState.STOPPED, VMState.STARTING),
    (VMState.STOPPED, VMState.TERMINATING),
    (VMState.STARTING, VMState.RUNNING),
    (VMState.STARTING, VMState.FAILED),
    (VMState.FAILED, VMState.TERMINATING),
    (VMState.TERMINATING, VMState.TERMINATED),
    (VMState.TERMINATING, VMState.FAILED),
}


@pytest.mark.parametrize("current,target", list(itertools.product(VMState, VMState)))
def test_every_state_pair_matches_the_documented_table(current: VMState, target: VMState) -> None:
    if (current, target) in LEGAL:
        assert_transition(current, target)
    else:
        with pytest.raises(InvalidTransition) as exc:
            assert_transition(current, target)
        assert exc.value.details["current_state"] == current.value


def test_terminated_is_terminal() -> None:
    assert TRANSITIONS[VMState.TERMINATED] == frozenset()


def test_failed_instance_can_only_be_cleaned_up() -> None:
    assert TRANSITIONS[VMState.FAILED] == {VMState.TERMINATING}


def test_error_message_names_the_action() -> None:
    with pytest.raises(InvalidTransition, match="cannot stop an instance in state STOPPED"):
        assert_transition(VMState.STOPPED, VMState.STOPPING, "stop")


def test_every_action_maps_to_a_legal_transitional_and_final_pair() -> None:
    for action, (transitional, final) in ACTION_STATES.items():
        assert final in TRANSITIONS[transitional], action
        assert VMState.FAILED in TRANSITIONS[transitional], action
        assert action in ACTION_DESIRED


def test_observed_corrections_only_touch_stable_states_and_restore_desired() -> None:
    for (recorded, _observed), corrected in OBSERVED_CORRECTIONS.items():
        assert recorded in {VMState.RUNNING, VMState.STOPPED}
        assert corrected in {VMState.RUNNING, VMState.STOPPED, VMState.FAILED}
    assert OBSERVED_CORRECTIONS[(VMState.RUNNING, ProviderVMState.STOPPED)] is VMState.STOPPED
    assert OBSERVED_CORRECTIONS[(VMState.RUNNING, None)] is VMState.FAILED
    assert RESTORE_ACTION[(VMState.STOPPED, DesiredState.RUNNING)] is OperationAction.START
    assert RESTORE_ACTION[(VMState.RUNNING, DesiredState.STOPPED)] is OperationAction.STOP
    # the restore action must be legal from the corrected state
    for (corrected, _desired), action in RESTORE_ACTION.items():
        assert ACTION_STATES[action][0] in TRANSITIONS[corrected]
