"""Appointment state transitions, as data.

Every write path asks this table before changing a status, so the lifecycle is stated
once and enumerated by tests rather than implied by scattered `if` statements. Pure: no
I/O, no session.

`confirmed` is reachable only from `pending` -- the headline invariant. The database
backs it independently (`ck_appointments_confirmed_requires_slot`), so a bug here and a
bug in the workflow would both have to happen for a slot-less confirmation to exist.
"""

from __future__ import annotations

import enum

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S
from app.modules.scheduling.models import VisitStatus

APPOINTMENT_TRANSITIONS: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.CONFIRMED, S.FAILED, S.CANCELLED}),
    S.CONFIRMED: frozenset({S.CANCELLED, S.RESCHEDULED, S.COMPLETED, S.NO_SHOW}),
}


def can_transition(source: S, target: S) -> bool:
    return target in APPOINTMENT_TRANSITIONS.get(source, frozenset())


def require_transition(source: S, target: S) -> None:
    """Raise `Conflict` for an illegal move. A conflict, not a validation error: the
    request was well-formed, the appointment is simply no longer in a state that allows
    it -- typically because something else moved it first."""
    if not can_transition(source, target):
        raise Conflict(
            f"an appointment cannot move from {source.value} to {target.value}"
        )


class NoShowOutcome(str, enum.Enum):
    """What the no-show timer found when it fired, settled under the appointment lock.

    Three answers, not a yes/no, because the two ways of *not* being a no-show mean
    different things: a visit row says a check-in reached the database (its signal to the
    workflow was lost or late), while `closed` says the appointment left `confirmed`
    entirely (cancelled, rescheduled, completed). Strings, so the visit workflow can
    return them as its result unchanged."""

    NO_SHOW = "no_show"
    VISIT_EXISTS = "visit_exists"
    CLOSED = "closed"


class VisitAction(str, enum.Enum):
    CHECK_IN = "check_in"
    SEEN = "seen"
    COMPLETE = "complete"


#: What each staff action requires, stated as data like the appointment table above.
#: check_in: appointment confirmed and no visit yet; seen: visit checked_in;
#: complete: visit in_progress.
VISIT_PRECONDITIONS: dict[VisitAction, VisitStatus | None] = {
    VisitAction.CHECK_IN: None,
    VisitAction.SEEN: VisitStatus.CHECKED_IN,
    VisitAction.COMPLETE: VisitStatus.IN_PROGRESS,
}
VISIT_RESULT: dict[VisitAction, VisitStatus] = {
    VisitAction.CHECK_IN: VisitStatus.CHECKED_IN,
    VisitAction.SEEN: VisitStatus.IN_PROGRESS,
    VisitAction.COMPLETE: VisitStatus.COMPLETED,
}


def require_visit_action(
    action: VisitAction, appointment: S, visit: VisitStatus | None
) -> None:
    """Raise Conflict unless `action` is legal now. Pure; the service supplies states.

    The appointment must be `confirmed` for every action: a cancelled or no-show
    appointment takes no visit actions, and a completed one has nothing left to do. The
    visit must then be exactly where the action expects it (`VISIT_PRECONDITIONS`)."""
    if appointment is not S.CONFIRMED or visit is not VISIT_PRECONDITIONS[action]:
        raise Conflict(
            f"cannot {action.value}: appointment is {appointment.value}, "
            f"visit is {visit.value if visit else 'none'}"
        )
