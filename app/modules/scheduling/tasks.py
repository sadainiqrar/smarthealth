"""Scheduling's Celery tasks: the stale-pending reconciler (closes PRD R-9).

A booking workflow that is *terminated* (from the Temporal UI or CLI) runs no workflow
code, so its compensation never runs: a `held` slot and a `pending` appointment are
stranded (R-9; the operational rule was "cancel, never terminate"). Every five minutes this
sweep finds `pending` appointments older than `STALE_AFTER` whose booking workflow is not
running -- closed, or never there -- and compensates them exactly as the workflow would
have: `fail_booking` (appointment failed, held slot free) and the billing release.

Safe to race a live workflow: `fail_booking` only acts on a `pending` row under its lock,
and a running workflow is skipped -- the sweep never decides for a booking still deciding.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from app.celery_app import TASKS, celery, task_settings
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import connect_client
from app.db.mongo import create_mongo_client, get_audit_collection
from app.modules.scheduling import service
from app.modules.scheduling.contracts import booking_workflow_id
from app.modules.scheduling.models import Appointment, AppointmentStatus
from app.settings import Settings

logger = logging.getLogger(__name__)

#: A booking takes seconds; one still pending after this long is either stuck or orphaned.
STALE_AFTER = timedelta(minutes=15)
ACTOR = "system:reconciler"


async def _workflow_running(client, appointment_id) -> bool:
    try:
        description = await client.get_workflow_handle(
            booking_workflow_id(appointment_id)
        ).describe()
    except RPCError:
        return False  # no such workflow: started nowhere, or long gone
    return description.status == WorkflowExecutionStatus.RUNNING


async def reconcile(settings: Settings, *, release_billing=None) -> list[str]:
    """Compensate every orphaned pending booking. Returns the appointment ids fixed.
    `release_billing(session, appointment_id)` is injected by the caller (the task wires
    billing's public interface in), so this module stays clear of billing."""
    clock = Clock()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mongo = create_mongo_client(settings)
    audit = AuditLog(get_audit_collection(mongo, settings), clock)
    client = await connect_client(settings, lazy=False)
    fixed: list[str] = []
    try:
        async with sessions() as session:
            candidates = list(await session.scalars(
                select(Appointment.id).where(
                    Appointment.status == AppointmentStatus.PENDING,
                    Appointment.created_at < clock.now() - STALE_AFTER,
                ).limit(200)
            ))
        for appointment_id in candidates:
            if await _workflow_running(client, appointment_id):
                continue
            async with sessions() as session:
                await service.fail_booking(
                    session, audit, appointment_id=appointment_id, actor_user_id=ACTOR
                )
                await session.commit()
            if release_billing is not None:
                async with sessions() as session:
                    await release_billing(session, appointment_id)
                    await session.commit()
            fixed.append(str(appointment_id))
            logger.warning("reconciled an orphaned pending booking",
                           extra={"appointment_id": str(appointment_id)})
    finally:
        await engine.dispose()
        await mongo.close()
    return fixed


@celery.task(name="scheduling.reconcile_stale_pending", acks_late=True)
def reconcile_stale_pending() -> int:
    from app.modules.billing.public import PaymentGateway, release

    settings = task_settings()
    gateway = PaymentGateway(
        settings.payments_url, timeout_seconds=settings.payments_timeout_seconds
    )

    async def release_billing(session, appointment_id):
        await release(session, gateway, appointment_id=appointment_id)

    return len(asyncio.run(reconcile(settings, release_billing=release_billing)))


TASKS.register("scheduling.reconcile_stale_pending", reconcile_stale_pending)
