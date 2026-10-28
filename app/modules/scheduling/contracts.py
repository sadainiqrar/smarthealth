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


@dataclass(frozen=True)
class VisitRequest:
    appointment_id: str


@dataclass(frozen=True)
class VisitSignal:
    """A staff action delivered to the visit workflow. `actor_user_id` is None for a
    system-originated one (the no-show race branch)."""

    actor_user_id: str | None


def visit_workflow_id(appointment_id: object) -> str:
    """Pure, so the workflow module can name the visit it starts without importing the
    Temporal client."""
    return f"visit-{appointment_id}"
