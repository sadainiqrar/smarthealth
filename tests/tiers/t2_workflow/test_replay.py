"""Replay safety (spec §8): a booking that was already in flight when slice 3 deployed
must still replay. The history is a real one, recorded from the slice-2 workflow by
`record_booking_history.py`; `workflow.patched("booking-starts-visit")` is what lets the
new child-start command coexist with it."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.modules.scheduling.workflows import BookAppointmentWorkflow, VisitWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

HISTORY = Path(__file__).parents[2] / "fixtures" / "histories" / "booking_pre_visit.json"


async def test_a_pre_visit_booking_history_still_replays():
    history = WorkflowHistory.from_json("booking-pre-visit", HISTORY.read_text(encoding="utf-8"))
    await Replayer(workflows=[BookAppointmentWorkflow, VisitWorkflow]).replay_workflow(history)
