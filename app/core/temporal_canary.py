"""The worker's readiness canary.

A workflow with no activities, whose only job is to be completed by a worker that is
polling the task queue. Executing it is the one readiness check that proves a worker
is not merely running but *working*: connected, polling, and able to execute.

This module is imported a second time inside Temporal's workflow sandbox, so it must
stay free of import-time side effects -- no registration here (see app/components.py).
"""

from __future__ import annotations

from temporalio import workflow

CANARY_REPLY = "ok"


@workflow.defn(name="Canary")
class CanaryWorkflow:
    @workflow.run
    async def run(self) -> str:
        return CANARY_REPLY
