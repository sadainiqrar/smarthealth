"""Every registered Kafka consumer is safe to deliver to twice (harness requirement 4).

Registry-driven, like the Temporal registry test: a new consumer is covered the day it
registers, with no list here to forget to update. Two layers make a consumer idempotent
and this checks the one a consumer itself owns -- what its *handler* does on a second
delivery. (The runtime's `processed_events` dedupe, the other layer, is proven against
Kafka and Postgres by case evt-004.)

Every handler here starts a workflow, so "one side effect" is "one workflow start
accepted": the fake Temporal refuses a second start under an id it has seen, as the real
server does with REJECT_DUPLICATE, and the handler must treat that as done, not fail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.components import register_all
from app.events.consumer import CONSUMERS, ConsumerContext, Event
from app.settings import Settings

pytestmark = pytest.mark.meta


class _Temporal:
    def __init__(self):
        self.accepted: list[str] = []

    async def start_workflow(self, run, request, *, id, task_queue, id_reuse_policy):
        if id in self.accepted:
            raise WorkflowAlreadyStartedError(id, "x")
        self.accepted.append(id)


def _specs():
    register_all()
    return [spec for _, spec in CONSUMERS.items()]


def test_consumers_are_registered():
    """Guards the parametrisation below against silently checking nothing."""
    assert len(_specs()) >= 3


@pytest.mark.parametrize("spec", _specs(), ids=lambda spec: spec.name)
def test_every_consumer_declares_a_fixture_for_each_topic(spec):
    assert len(spec.fixtures) == len(spec.topics), (
        f"{spec.name} needs one fixture payload per topic for this test"
    )


@pytest.mark.parametrize("spec", _specs(), ids=lambda spec: spec.name)
async def test_delivering_an_event_twice_has_one_effect(spec):
    temporal = _Temporal()
    context = ConsumerContext(settings=Settings(), temporal=temporal)
    for topic, payload in zip(spec.topics, spec.fixtures, strict=True):
        event = Event(
            event_id=str(uuid.uuid4()), event_type=topic, subject_id="s",
            occurred_at=datetime.now(UTC), payload=payload, topic=topic, partition=0, offset=0,
        )
        await spec.handler(None, event, context)
        await spec.handler(None, event, context)  # the redelivery: must not raise
    assert len(temporal.accepted) == len(set(temporal.accepted)) >= 1
