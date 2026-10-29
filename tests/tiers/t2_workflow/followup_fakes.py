"""Fakes for the cancel follow-up and reschedule workflows.

Every activity those workflows (and the bookings they run as children) call is replaced by
a recording fake with the real name. A confirmed booking starts a `Visit` child, so the
`visit.*` fakes from `visit_fakes` are registered too, and `World.confirmed` remembers
every appointment that confirmed so a test can end each visit inside the worker context
(tests/README, "Temporal time-skipping pitfalls").
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import (
    BookingRequest,
    FollowUpRequest,
    RescheduleRequest,
    VisitRequest,
    visit_workflow_id,
)
from app.modules.scheduling.workflows import (
    BookAppointmentWorkflow,
    CancelFollowUpWorkflow,
    RescheduleWorkflow,
    VisitWorkflow,
)
from tests.tiers.t2_workflow.visit_fakes import finish_visit, open_visit_fakes

BOOKING_STEPS = [
    "create_pending", "validate_booking", "claim_slot", "billing_precheck",
    "schedule_reminders", "confirm", "fail_booking",
]


@dataclass
class World:
    """What the fakes answer, and what they saw."""

    #: `[[entry_id, patient_id], ...]`, as the real `waitlist.candidates` returns them.
    candidates: list[list[str]] = field(default_factory=list)
    #: A booking for one of these patients loses its slot race at `claim_slot`.
    losing_patients: set[str] = field(default_factory=set)
    reschedule_outcome: str = "rescheduled"
    released_slot_id: str | None = "released-slot"
    calls: list[str] = field(default_factory=list)
    candidate_queries: list[str] = field(default_factory=list)
    refunds: list[FollowUpRequest] = field(default_factory=list)
    reschedules: list[RescheduleRequest] = field(default_factory=list)
    fulfilled: list[str] = field(default_factory=list)
    #: Every booking request that confirmed, in order.
    confirmed: list[BookingRequest] = field(default_factory=list)
    #: Every booking request that reached `create_pending`, in order.
    bookings: list[BookingRequest] = field(default_factory=list)


def fakes(world: World):
    def booking(name: str):
        @activity.defn(name=f"booking.{name}")
        async def fake(request: BookingRequest) -> None:
            world.calls.append(name)
            if name == "create_pending":
                world.bookings.append(request)
            if name == "claim_slot" and request.patient_id in world.losing_patients:
                raise ApplicationError("slot taken", type="Conflict", non_retryable=True)
            if name == "confirm":
                world.confirmed.append(request)

        return fake

    @activity.defn(name="cancel.refund")
    async def refund(request: FollowUpRequest) -> None:
        world.calls.append("refund")
        world.refunds.append(request)

    @activity.defn(name="reschedule.complete")
    async def reschedule_complete(request: RescheduleRequest) -> dict[str, str | None]:
        world.calls.append("reschedule_complete")
        world.reschedules.append(request)
        rescheduled = world.reschedule_outcome == "rescheduled"
        return {
            "outcome": world.reschedule_outcome,
            "released_slot_id": world.released_slot_id if rescheduled else None,
        }

    @activity.defn(name="waitlist.candidates")
    async def candidates(slot_id: str) -> list[list[str]]:
        world.calls.append("candidates")
        world.candidate_queries.append(slot_id)
        return world.candidates

    @activity.defn(name="waitlist.fulfil")
    async def fulfil(entry_id: str) -> None:
        world.calls.append("fulfil")
        world.fulfilled.append(entry_id)

    return [
        *(booking(name) for name in BOOKING_STEPS),
        refund, reschedule_complete, candidates, fulfil,
    ]


@contextlib.asynccontextmanager
async def follow_up_worker(env, world: World):
    """A worker for every workflow here and the fakes, yielding `(queue, release)`.
    `release` unparks the held `visit.deadline` activity; see `finish_visits`."""
    release = asyncio.Event()
    queue = f"t2-followup-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        env.client, queue,
        workflows=[
            BookAppointmentWorkflow, VisitWorkflow, CancelFollowUpWorkflow,
            RescheduleWorkflow,
        ],
        activities=[*fakes(world), *open_visit_fakes(env, release)],
    ):
        yield queue, release


async def start_visit(env, queue: str, appointment_id: str) -> None:
    """A visit timer for an appointment the test did not book through a workflow."""
    await env.client.start_workflow(
        VisitWorkflow.run, VisitRequest(appointment_id),
        id=visit_workflow_id(appointment_id), task_queue=queue,
    )


async def finish_visits(env, release: asyncio.Event, appointment_ids) -> dict[str, str]:
    """Cancel every listed visit and wait for each to end; its result by appointment id."""
    return {
        appointment_id: await finish_visit(env, appointment_id, release)
        for appointment_id in appointment_ids
    }
