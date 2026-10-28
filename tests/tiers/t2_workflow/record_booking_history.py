"""Record the booking workflow's event history, for replay checks.

A script, not a test: run it by hand with
`python -m tests.tiers.t2_workflow.record_booking_history`. The output
(`tests/fixtures/histories/booking_pre_visit.json`) is what the slice-2 booking workflow
produced *before* it was changed to start a visit workflow. Replaying it against the
current workflow is the proof that an in-flight booking survives the change
(`workflow.patched`). Re-recording after the workflow changes would defeat the point --
the file is the "before".

The `__main__` guard matters: Temporal's sandbox re-imports modules, and an unguarded
script would run the recording again inside it.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

OUTPUT = Path(__file__).resolve().parents[2] / "fixtures" / "histories" / "booking_pre_visit.json"

STEPS = [
    "create_pending", "validate_booking", "claim_slot",
    "billing_precheck", "schedule_reminders", "confirm", "fail_booking",
]


def _fakes() -> list:
    def make(name: str):
        @activity.defn(name=f"booking.{name}")
        async def fake(request: BookingRequest) -> None:
            return None

        return fake

    return [make(name) for name in STEPS]


async def record() -> str:
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = "record-booking-history"
        async with build_worker(
            env.client, queue, workflows=[BookAppointmentWorkflow], activities=_fakes()
        ):
            handle = await env.client.start_workflow(
                BookAppointmentWorkflow.run, request,
                id=f"booking-{request.appointment_id}", task_queue=queue,
            )
            assert await handle.result() == "confirmed"
            history = await handle.fetch_history()
    return history.to_json()


if __name__ == "__main__":
    text = asyncio.run(record())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")
    print(f"wrote {OUTPUT} ({len(text)} bytes)")
