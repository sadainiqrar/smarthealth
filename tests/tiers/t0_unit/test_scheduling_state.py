import pytest

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S
from app.modules.scheduling.state import APPOINTMENT_TRANSITIONS, can_transition, require_transition

pytestmark = pytest.mark.unit

ALLOWED = {
    (S.PENDING, S.CONFIRMED), (S.PENDING, S.FAILED), (S.PENDING, S.CANCELLED),
    (S.CONFIRMED, S.CANCELLED), (S.CONFIRMED, S.RESCHEDULED),
    (S.CONFIRMED, S.COMPLETED), (S.CONFIRMED, S.NO_SHOW),
}


@pytest.mark.parametrize("source", list(S))
@pytest.mark.parametrize("target", list(S))
def test_every_pair_is_allowed_exactly_when_listed(source, target):
    assert can_transition(source, target) is ((source, target) in ALLOWED)


def test_confirmed_is_reachable_only_from_pending():
    """The headline invariant, stated as data."""
    assert {s for s, targets in APPOINTMENT_TRANSITIONS.items() if S.CONFIRMED in targets} == {
        S.PENDING
    }


def test_terminal_states_have_no_exit():
    for terminal in (S.CANCELLED, S.RESCHEDULED, S.COMPLETED, S.NO_SHOW, S.FAILED):
        assert not APPOINTMENT_TRANSITIONS.get(terminal)


def test_an_illegal_transition_is_a_conflict_naming_both_states():
    with pytest.raises(Conflict, match="completed.*confirmed"):
        require_transition(S.COMPLETED, S.CONFIRMED)
