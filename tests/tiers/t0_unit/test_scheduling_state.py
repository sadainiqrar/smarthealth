import pytest

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S
from app.modules.scheduling.models import VisitStatus
from app.modules.scheduling.state import (
    APPOINTMENT_TRANSITIONS,
    VisitAction,
    can_transition,
    require_transition,
    require_visit_action,
)

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


# --- visit lifecycle ----------------------------------------------------------------

V = VisitStatus
LEGAL_VISIT_ACTIONS = {
    (VisitAction.CHECK_IN, S.CONFIRMED, None),
    (VisitAction.SEEN, S.CONFIRMED, V.CHECKED_IN),
    (VisitAction.COMPLETE, S.CONFIRMED, V.IN_PROGRESS),
}


@pytest.mark.parametrize("action", list(VisitAction))
@pytest.mark.parametrize("appointment", list(S))
@pytest.mark.parametrize("visit", [None, *V])
def test_a_visit_action_is_legal_exactly_for_the_three_listed_triples(
    action, appointment, visit
):
    if (action, appointment, visit) in LEGAL_VISIT_ACTIONS:
        require_visit_action(action, appointment, visit)
    else:
        with pytest.raises(Conflict):
            require_visit_action(action, appointment, visit)


def test_a_refused_visit_action_names_the_action_and_both_states():
    with pytest.raises(Conflict, match="seen.*cancelled.*none"):
        require_visit_action(VisitAction.SEEN, S.CANCELLED, None)
    with pytest.raises(Conflict, match="complete.*confirmed.*checked_in"):
        require_visit_action(VisitAction.COMPLETE, S.CONFIRMED, V.CHECKED_IN)
