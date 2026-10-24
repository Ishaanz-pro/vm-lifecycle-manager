import itertools

import pytest

from vmlm.domain.errors import InvalidTransition
from vmlm.domain.states import TRANSITIONS, VMState, assert_transition

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
