"""A workflow run joins the trace of whoever started it.

Without this the trace of `POST /appointments` would end at "start workflow", and the
booking's activities -- where every interesting failure happens -- would each be a trace
of their own that nothing links back to the request.
"""

import uuid

import pytest
from temporalio.client import Client

from app.core.temporal import build_worker, tracing_interceptors
from app.core.temporal_canary import CanaryWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


async def test_starting_and_running_a_workflow_share_one_trace(temporal_env, spans):
    config = temporal_env.client.config()
    config["interceptors"] = tracing_interceptors()
    client = Client(**config)
    queue = f"tracing-{uuid.uuid4()}"
    async with build_worker(client, queue):
        await client.execute_workflow(
            CanaryWorkflow.run, id=f"canary-{uuid.uuid4()}", task_queue=queue
        )
    by_name = {span.name: span for span in spans.get_finished_spans()}
    start, run = by_name["StartWorkflow:Canary"], by_name["RunWorkflow:Canary"]
    assert start.context.trace_id == run.context.trace_id
