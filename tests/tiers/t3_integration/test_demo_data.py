"""Demo data against a real database: every constraint holds, re-runs add nothing, the
analytics reconcile finds no drift, and the live phase books through real workflows."""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import UTC, timedelta

import pytest
from sqlalchemy import func, select

from app.components import build_activities, register_all
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.ratelimit import InMemoryRateLimiter
from app.core.temporal import TASK_QUEUE, WORKFLOWS, build_worker, connect_client
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.demo.history import ORIGIN, day_imported, import_day, last_imported_day, plan_day
from app.demo.live import run_live
from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    build_network,
    ensure_slots,
    patient_identity,
)
from app.modules.analytics.tasks import reconcile
from app.modules.billing.models import BillingRecord, BillingStatus
from app.events.models import OutboxEvent
from app.modules.clinical.models import (
    ClinicalNote,
    NoteStatus,
    OrderStatus,
    PatientDocument,
    TestOrder,
)
from app.modules.clinical.storage import FilesystemDocumentStore
from app.modules.identity.service import authenticate
from app.modules.identity.sessions import InMemorySessionStore
from app.modules.patients.models import Patient
from app.modules.scheduling.contracts import visit_workflow_id
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
)
from tests.tiers.t3_integration.demo_helpers import purge_demo

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PASSWORD = "demo-integration-password"


@pytest.fixture
async def demo_factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        await purge_demo(session)
    try:
        yield factory
    finally:
        async with factory() as session:
            await purge_demo(session)
        await engine.dispose()


async def test_the_network_is_idempotent_and_its_logins_authenticate(demo_factory, db_settings):
    async with demo_factory() as session:
        net, created = await build_network(session, password=PASSWORD)
        await session.commit()
    assert created["providers"] == len(PROVIDERS) and created["patients"] == PATIENT_COUNT
    async with demo_factory() as session:
        again, created_again = await build_network(session, password=PASSWORD)
        await session.commit()
    assert set(created_again.values()) == {0}
    assert again.provider_ids == net.provider_ids
    async with demo_factory() as session:
        email = patient_identity(LINKED_PATIENT_INDEXES[0]).email
        tokens = await authenticate(
            session, email=email, password=PASSWORD, client_ip="127.0.0.1",
            sessions=InMemorySessionStore(), limiter=InMemoryRateLimiter(),
            settings=db_settings, now=Clock().now(),
        )
        assert tokens.access_token
        linked = await session.scalar(
            select(func.count()).select_from(Patient).where(Patient.user_id.is_not(None),
                                                            Patient.mrn.like("DEMO-%")))
        assert linked == len(LINKED_PATIENT_INDEXES)


async def test_upcoming_slots_follow_the_schedule_and_are_not_duplicated(demo_factory):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        today = Clock().now().date()
        made = await ensure_slots(session, net, first_day=today,
                                  last_day=today + timedelta(days=13))
        await session.commit()
        assert made > 0
        again = await ensure_slots(session, net, first_day=today,
                                   last_day=today + timedelta(days=13))
        assert again == 0
        count = await session.scalar(select(func.count()).select_from(ProviderSlot).where(
            ProviderSlot.provider_id.in_(net.provider_ids.values())))
        assert count == made


async def _import(demo_factory, db_settings, days):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        await session.commit()
    store = FilesystemDocumentStore.from_settings(db_settings)
    today = Clock().now().astimezone(net.tz).date()
    for day in days:
        async with demo_factory() as session:
            if await day_imported(session, net, day):
                continue
            await import_day(session, net, plan_day(day, tz=net.tz, today=today),
                             store=store, settings=db_settings, now=Clock().now())
            await session.commit()
    return net, today


async def test_imported_history_holds_every_rule_and_is_marked(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(30, 20, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        ids = list(net.provider_ids.values())
        appts = (await session.scalars(select(Appointment).where(
            Appointment.provider_id.in_(ids)))).all()
        assert appts and {a.origin for a in appts} == {ORIGIN}
        assert {a.status for a in appts} <= {AppointmentStatus.COMPLETED,
                                             AppointmentStatus.CANCELLED,
                                             AppointmentStatus.NO_SHOW}
        completed = [a for a in appts if a.status is AppointmentStatus.COMPLETED]
        visits = (await session.scalars(select(Visit).where(
            Visit.appointment_id.in_([a.id for a in completed])))).all()
        assert len(visits) == len(completed)
        notes = (await session.scalars(select(ClinicalNote).where(
            ClinicalNote.appointment_id.in_([a.id for a in completed])))).all()
        assert len(notes) == len(completed) and {n.status for n in notes} == {NoteStatus.SIGNED}
        billing = {b.appointment_id: b.status for b in (await session.scalars(
            select(BillingRecord).where(BillingRecord.appointment_id.in_([a.id for a in appts]))
        )).all()}
        assert len(billing) == len(appts)
        for a in appts:
            expected = (BillingStatus.CAPTURED if a.status is AppointmentStatus.COMPLETED
                        else BillingStatus.VOIDED)
            assert billing[a.id] is expected
        docs = (await session.scalars(select(PatientDocument).where(
            PatientDocument.patient_id.in_(net.patient_ids)))).all()
        assert all(d.extraction_status == "extracted" and d.extracted_text for d in docs)
        # Slot status agrees with the appointment: booked iff completed or no-show.
        slot_status = {s.id: s.status for s in (await session.scalars(select(ProviderSlot).where(
            ProviderSlot.id.in_([a.slot_id for a in appts])))).all()}
        for a in appts:
            expected = (SlotStatus.FREE if a.status is AppointmentStatus.CANCELLED
                        else SlotStatus.BOOKED)
            assert slot_status[a.slot_id] is expected
        # No outbox rows: nobody is emailed about the past.
        keys = {str(a.id) for a in appts} | {str(a.patient_id) for a in appts}
        assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.key.in_(keys))) == 0
        # Every reviewed order's result links both ways and its bytes are in the store.
        store = FilesystemDocumentStore.from_settings(db_settings)
        reviewed = (await session.scalars(select(TestOrder).where(
            TestOrder.patient_id.in_(net.patient_ids),
            TestOrder.status == OrderStatus.REVIEWED))).all()
        by_id = {d.id: d for d in docs}
        for order in reviewed:
            document = by_id[order.result_document_id]
            assert document.test_order_id == order.id
            assert await store.get(document.object_key)


async def test_importing_again_adds_nothing(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(15, 10, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        before = await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values())))
    await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        after = await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values())))
        assert after == before
        assert await last_imported_day(session, net, now=Clock().now()) == max(
            d for d in days if d.weekday() < 5)


async def test_the_nightly_reconcile_finds_no_drift_in_imported_days(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(40, 9, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    # Bookings count on their confirmation day, up to 21 days before the visit, so check a
    # window that starts after the earliest imported confirmation could fall outside it.
    drift = await reconcile(db_settings, start=today - timedelta(days=19),
                            end=today - timedelta(days=11))
    demo_clinics = {str(c) for c in net.clinic_ids.values()}
    assert [d for d in drift if d["clinic_id"] in demo_clinics] == []


async def test_days_that_once_held_upcoming_slots_are_imported_around_live_bookings(
        demo_factory, db_settings):
    today = Clock().now().astimezone(UTC).date()
    first, last = today - timedelta(days=20), today - timedelta(days=16)
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        await ensure_slots(session, net, first_day=first, last_day=last)
        kept = (await session.scalars(select(ProviderSlot).where(
            ProviderSlot.provider_id.in_(net.provider_ids.values()))
            .order_by(ProviderSlot.starts_at).limit(1))).one()
        session.add(Appointment(
            patient_id=net.patient_ids[0], provider_id=kept.provider_id,
            clinic_id=kept.clinic_id, department_id=kept.department_id, slot_id=kept.id,
            status=AppointmentStatus.CANCELLED, origin="front_desk",
            cancelled_at=kept.starts_at - timedelta(days=1), cancellation_reason="test"))
        await session.commit()
        kept_id, kept_start, kept_end = kept.id, kept.starts_at, kept.ends_at
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    net, _ = await _import(demo_factory, db_settings, days)

    async def count(session):
        return await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values()),
            Appointment.origin == ORIGIN))

    async with demo_factory() as session:
        weekdays = [d for d in days if d.weekday() < 5]
        assert all([await day_imported(session, net, d) for d in weekdays])
        before = await count(session)
        assert before > 0
        assert await session.get(ProviderSlot, kept_id) is not None
        overlapping = await session.scalar(select(func.count()).select_from(ProviderSlot).where(
            ProviderSlot.provider_id == kept.provider_id,
            ProviderSlot.starts_at < kept_end, ProviderSlot.ends_at > kept_start))
        assert overlapping == 1
    await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        assert await count(session) == before


@pytest.fixture
async def demo_worker(db_settings, demo_factory):
    register_all()
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(demo_factory, audit, Clock(), timedelta(minutes=15),
                                    settings=db_settings))
    try:
        async with worker:
            yield
            # The visit timers booking started would otherwise run on, past this test.
            async with demo_factory() as session:
                ids = (await session.scalars(
                    select(Appointment.id).join(Patient, Patient.id == Appointment.patient_id)
                    .where(Patient.mrn.like("DEMO-%")))).all()
            for appointment_id in ids:
                with contextlib.suppress(Exception):
                    await client.get_workflow_handle(visit_workflow_id(appointment_id)).cancel()
    finally:
        await mongo.close()


async def test_live_activity_goes_through_the_real_workflows(api, demo_factory, demo_worker):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        today = Clock().now().astimezone(net.tz).date()
        await ensure_slots(session, net, first_day=today + timedelta(days=1),
                           last_day=today + timedelta(days=14))
        await session.commit()
    summary = await run_live(api, net, password=PASSWORD, target=4, existing_upcoming=0,
                             existing_waitlist=0, now=Clock().now())
    assert summary.booked >= 3 and summary.cancelled == 1 and summary.rescheduled == 1
    assert summary.waitlisted == 2
    async with demo_factory() as session:
        live = (await session.scalars(select(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values()),
            Appointment.origin.in_(("front_desk", "web"))))).all()
        statuses = Counter(a.status for a in live)
        assert statuses[AppointmentStatus.CONFIRMED] >= 2
        assert statuses[AppointmentStatus.CANCELLED] == 1
        assert statuses[AppointmentStatus.RESCHEDULED] == 1
