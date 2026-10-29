"""Values passed between the booking workflow and its activities.

Plain dataclasses of strings: Temporal serialises them as JSON, and a type the default
converter cannot encode (uuid.UUID) would fail at runtime, not import time. Pure, so the
workflow sandbox imports it directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookingRequest:
    appointment_id: str
    patient_id: str
    slot_id: str
    actor_user_id: str | None
    reason: str | None = None
    #: Set only for the new appointment of a reschedule. Last and defaulted, so a request
    #: recorded in a workflow history before it existed still decodes.
    rescheduled_from_id: str | None = None


@dataclass(frozen=True)
class FollowUpRequest:
    """What the cancel follow-up needs to know."""

    appointment_id: str  # the cancelled (or rescheduled-away) appointment
    slot_id: str | None  # the slot it released, if any
    actor_user_id: str | None


@dataclass(frozen=True)
class RescheduleRequest:
    old_appointment_id: str
    new_appointment_id: str
    patient_id: str
    new_slot_id: str
    actor_user_id: str | None
    reason: str | None = None


@dataclass(frozen=True)
class VisitRequest:
    appointment_id: str


def visit_workflow_id(appointment_id: object) -> str:
    """Pure, so the workflow module can name the visit it starts without importing the
    Temporal client."""
    return f"visit-{appointment_id}"
