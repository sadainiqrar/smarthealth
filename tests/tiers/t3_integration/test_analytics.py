"""Analytics against real Kafka-shaped events, Postgres and the HTTP API (PART-A-FR-5).

The consumer is driven through the runtime's `apply` -- the same dedupe-then-handle
transaction it runs in production -- with events for a clinic and provider of this test's
own, so the numbers asserted belong to nobody else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.consumer import ConsumerRunner, Event, Outcome
from app.modules.analytics.consumers import ANALYTICS_CONSUMERS
from app.modules.analytics.tasks import reconcile
from app.modules.identity.models import UserRole
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]

DAY = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


def _event(topic: str, payload: dict, at: datetime = DAY) -> Event:
    return Event(event_id=str(uuid.uuid4()), event_type=topic, subject_id="s", occurred_at=at,
                 payload=payload, topic=topic, partition=0, offset=0)


async def test_events_are_counted_once_and_served_by_the_api(factory, db_settings, api, token_for):
    """Referenced by tests/cases/anl-001-the-five-metrics-come-from-events-counted-once.yaml."""
    runner = ConsumerRunner(db_settings, factory, ANALYTICS_CONSUMERS[0])
    clinic, provider = str(uuid.uuid4()), str(uuid.uuid4())
    base = {"clinic_id": clinic, "provider_id": provider}
    booked = [_event("appointments.booked", base) for _ in range(4)]
    cancelled = _event("appointments.cancelled", base)
    completed = _event("visits.completed", {
        **base, "checked_in_at": (DAY - timedelta(minutes=20)).isoformat(),
        "seen_at": (DAY - timedelta(minutes=5)).isoformat(), "completed_at": DAY.isoformat(),
    })
    no_show = _event("appointments.no_show", base)
    for event in (*booked, cancelled, completed, no_show):
        assert await runner.apply(event) is Outcome.APPLIED
    # Redelivered: none of these may count again.
    assert await runner.apply(booked[0]) is Outcome.DUPLICATE
    assert await runner.apply(completed) is Outcome.DUPLICATE

    response = await api.get(
        "/analytics/summary",
        params={"from": "2026-03-01", "to": "2026-03-03", "clinic_id": clinic},
        headers=token_for(UserRole.ADMIN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["appointments_booked"], body["cancellations"], body["completed_visits"],
            body["no_shows"]) == (4, 1, 1, 1)
    assert body["cancellation_rate"] == 0.25
    assert body["average_wait_seconds"] == 15 * 60
    assert body["total_patients"] >= 0

    series = await api.get(
        "/analytics/timeseries",
        params={"metric": "booked", "from": "2026-03-01", "to": "2026-03-03", "clinic_id": clinic},
        headers=token_for(UserRole.FRONT_DESK),
    )
    assert series.json()["points"] == [{"day": "2026-03-02", "value": 4.0}]


async def test_a_visit_never_seen_counts_but_adds_no_wait(factory, db_settings, api, token_for):
    runner = ConsumerRunner(db_settings, factory, ANALYTICS_CONSUMERS[0])
    clinic = str(uuid.uuid4())
    await runner.apply(_event("visits.completed", {
        "clinic_id": clinic, "provider_id": str(uuid.uuid4()),
        "checked_in_at": DAY.isoformat(), "seen_at": None, "completed_at": DAY.isoformat(),
    }))
    body = (await api.get(
        "/analytics/summary",
        params={"from": "2026-03-01", "to": "2026-03-03", "clinic_id": clinic},
        headers=token_for(UserRole.ADMIN),
    )).json()
    assert body["completed_visits"] == 1 and body["average_wait_seconds"] is None


async def test_the_reconcile_reports_a_booking_the_rollup_never_counted(factory, db_settings):
    """Referenced by tests/cases/anl-002-the-nightly-reconcile-finds-drift.yaml.

    The appointment confirms through the service (its event is written, but no consumer
    runs here), so the rollup lacks it: exactly the lost-event drift the reconcile exists
    to catch. Then the consumer applies a booked event for it and the drift is gone."""
    b, appointment_id = await confirmed_appointment(factory, RecordingAudit())
    today = datetime.now(UTC).date()
    drift = await reconcile(db_settings, start=today, end=today)
    mine = [c for c in drift if c["provider_id"] == str(b.provider_id)]
    assert mine == [{"metric": "booked", "day": today.isoformat(), "clinic_id": str(b.clinic_id),
                     "provider_id": str(b.provider_id), "expected": 1, "actual": 0}]

    runner = ConsumerRunner(db_settings, factory, ANALYTICS_CONSUMERS[0])
    await runner.apply(_event("appointments.booked", {
        "clinic_id": str(b.clinic_id), "provider_id": str(b.provider_id)},
        at=datetime.now(UTC)))
    drift = await reconcile(db_settings, start=today, end=today)
    assert not [c for c in drift if c["provider_id"] == str(b.provider_id)]
