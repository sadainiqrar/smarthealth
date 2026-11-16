"""Demo data for a deployed SmartHealth: an imported history plus live activity.

`demo_seed` builds the network, imports six months of history up to yesterday, opens two
weeks of slots and books live activity through the API. `demo_refresh` brings an existing
demo up to date: history from the last imported day to yesterday, slots two weeks ahead,
live bookings topped up. Both are idempotent and commit one history day at a time.

Spec: docs/superpowers/specs/2026-11-16-demo-seed-data-design.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditEvent
from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.demo.history import (
    ACTOR,
    HISTORY_DAYS,
    DayCounts,
    day_imported,
    import_day,
    last_imported_day,
    plan_day,
)
from app.demo.live import LiveSummary, live_state, run_live
from app.demo.network import (
    DEFAULT_TIMEZONE,
    MRN_PREFIX,
    UPCOMING_DAYS,
    Network,
    build_network,
    ensure_slots,
)
from app.modules.clinical.storage import FilesystemDocumentStore
from app.modules.patients.models import Patient
from app.settings import Settings


class DemoRefused(Exception):
    """The database holds data that is not the demo's; refusing to mix fiction into it."""


@dataclass
class DemoSummary:
    created: dict[str, int] = field(default_factory=dict)
    slots: int = 0
    days_imported: int = 0
    history: DayCounts = field(default_factory=DayCounts)
    live: LiveSummary | None = None

    @property
    def failed(self) -> int:
        """Live bookings or reschedules that did not confirm (the CLI exits non-zero)."""
        return self.live.failed if self.live else 0

    @property
    def failures(self) -> list[str]:
        return list(self.live.failures) if self.live else []


async def foreign_patients(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Patient).where(
        Patient.mrn.not_like(f"{MRN_PREFIX}%")))


async def demo_seed(*, settings: Settings, password: str, api_url: str, audit,
                    timezone: str = DEFAULT_TIMEZONE, http: httpx.AsyncClient | None = None,
                    clock: Clock | None = None) -> DemoSummary:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            others = await foreign_patients(session)
            if others:
                raise DemoRefused(
                    f"{others} patient(s) here are not demo patients (MRN not {MRN_PREFIX}...); "
                    "demo-seed only runs on a database it owns")
            net, created = await build_network(session, password=password, timezone=timezone)
            await session.commit()
        return await _top_up(factory, net, created, settings=settings, password=password,
                             api_url=api_url, audit=audit, http=http, clock=clock)
    finally:
        await engine.dispose()


async def demo_refresh(*, settings: Settings, password: str, api_url: str, audit,
                       timezone: str = DEFAULT_TIMEZONE, http: httpx.AsyncClient | None = None,
                       clock: Clock | None = None) -> DemoSummary:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            net, created = await build_network(session, password=password, timezone=timezone)
            await session.commit()
        return await _top_up(factory, net, created, settings=settings, password=password,
                             api_url=api_url, audit=audit, http=http, clock=clock)
    finally:
        await engine.dispose()


async def _top_up(factory: async_sessionmaker[AsyncSession], net: Network,
                  created: dict[str, int], *, settings: Settings, password: str, api_url: str,
                  audit, http: httpx.AsyncClient | None, clock: Clock | None) -> DemoSummary:
    now = (clock or Clock()).now()
    today = now.astimezone(net.tz).date()
    summary = DemoSummary(created=created)

    async with factory() as session:
        summary.slots = await ensure_slots(session, net, first_day=today + timedelta(days=1),
                                           last_day=today + timedelta(days=UPCOMING_DAYS))
        await session.commit()

    async with factory() as session:
        last = await last_imported_day(session, net, now=now)
    first = (last + timedelta(days=1)) if last else today - timedelta(days=HISTORY_DAYS)
    store = FilesystemDocumentStore.from_settings(settings)
    imported: list[date] = []
    day = first
    while day < today:
        async with factory() as session:
            if not await day_imported(session, net, day):
                counts = await import_day(session, net, plan_day(day, tz=net.tz, today=today),
                                          store=store, settings=settings, now=now)
                await session.commit()
                summary.history.add(counts)
                if counts.slots:
                    imported.append(day)
        day += timedelta(days=1)
    summary.days_imported = len(imported)
    if imported:
        await audit.record(AuditEvent(
            entity_type="demo_history", entity_id=f"{imported[0]}..{imported[-1]}",
            action="imported", actor_user_id=ACTOR, after=asdict(summary.history)))

    async with factory() as session:
        state = await live_state(session, net, now)
    owned = http is None
    client = http or httpx.AsyncClient(base_url=api_url, timeout=30)
    try:
        summary.live = await run_live(
            client, net, password=password, now=now, existing_upcoming=state.upcoming,
            existing_waitlist=state.waitlist, busy_patient_ids=state.busy_patient_ids,
            waiting_patient_ids=state.waiting_patient_ids)
    finally:
        if owned:
            await client.aclose()
    return summary


__all__ = ["DemoRefused", "DemoSummary", "demo_refresh", "demo_seed"]
