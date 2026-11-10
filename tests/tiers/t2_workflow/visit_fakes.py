"""Visit-activity fakes for booking tests.

A confirmed booking starts a child `Visit` workflow on the same queue, so every booking
test's worker needs the `visit.*` activities registered and must end that child inside
the worker context: one left running on a queue nobody polls stops the time-skipping
server from skipping for the rest of the session.

The `deadline` activity is held until the test releases it. A child waiting on its
no-show timer would be fired by the time-skipping server the moment the booking finishes
-- racing the test's own assertions about the child -- whereas one parked in an activity
cannot be. `finish_visit` cancels the child, releases it, and waits for it to end.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity

from app.modules.scheduling.contracts import VisitRequest, visit_workflow_id


def reminder_fakes(calls: list[str] | None = None, *, due=None):
    """The reminder's two activities (Week 3 slice 5). By default `reminder_due` answers
    None -- no reminder -- so a test about something else sees the workflow it always did.
    `due` is an async callable returning the ISO time to remind at."""

    @activity.defn(name="visit.reminder_due")
    async def reminder_due(request: VisitRequest) -> str | None:
        if calls is not None:
            calls.append("reminder_due")
        return await due() if due else None

    @activity.defn(name="visit.remind")
    async def remind(request: VisitRequest) -> str:
        if calls is not None:
            calls.append("remind")
        return "queued"

    return [reminder_due, remind]


def open_visit_fakes(env, release: asyncio.Event):
    @activity.defn(name="visit.deadline")
    async def deadline(request: VisitRequest) -> str:
        await release.wait()
        # The server's virtual clock, not the wall clock (see test_visit_workflow).
        return ((await env.get_current_time()) + timedelta(hours=2)).isoformat()

    @activity.defn(name="visit.no_show")
    async def no_show(request: VisitRequest) -> str:
        return "no_show"

    return [deadline, no_show, *reminder_fakes()]


async def finish_visit(env, appointment_id: str, release: asyncio.Event) -> str:
    """Cancel the child visit a confirmed booking started and wait for it to end."""
    handle = env.client.get_workflow_handle(visit_workflow_id(appointment_id))
    await handle.signal("cancel")
    release.set()
    return await handle.result()
