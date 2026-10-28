"""Appointment state transitions, as data.

Every write path asks this table before changing a status, so the lifecycle is stated
once and enumerated by tests rather than implied by scattered `if` statements. Pure: no
I/O, no session.

`confirmed` is reachable only from `pending` -- the headline invariant. The database
backs it independently (`ck_appointments_confirmed_requires_slot`), so a bug here and a
bug in the workflow would both have to happen for a slot-less confirmation to exist.
"""

from __future__ import annotations

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S

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
