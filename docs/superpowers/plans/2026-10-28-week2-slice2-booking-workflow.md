# Week 2 Slice 2 — Booking Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /appointments` books an appointment through a Temporal workflow that confirms it **only after** validation, slot claim, billing pre-check and reminder scheduling all succeed, and compensates — slot back to `free`, appointment `failed` — on any failure. `GET /appointments/{id}` shows the result.

**Architecture:** `ProviderSlot` moves into `scheduling` (D-9). `scheduling/service.py` holds the database operations, each idempotent and each slot write paired with the appointment write in one transaction. `scheduling/activities.py` wraps one service call per activity in its own unit of work and turns domain errors into non-retryable Temporal errors. `scheduling/workflows.py` sequences the activities and compensates. `scheduling/booking.py` derives ids and starts the workflow with policies that make a repeat request map to the same booking. The router returns 202. Patient checks go through `patients/public.py` (D-15), called from activities and the router — never from `service.py`.

**Tech Stack:** temporalio 1.34, SQLAlchemy 2 async + asyncpg, FastAPI, pytest-asyncio 1.4.

**Spec:** `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §2 (D-9, D-10, D-11, D-15), §4.1, §5, §6, §7. Slice 1 (Temporal worker) has landed on `main`.

**Branch:** `feat/booking-workflow` from `main`.

---

## Facts the implementer needs (verified 2026-10-28, not recalled)

1. **Workflow-id policies.** By default Temporal lets a *closed* workflow id be started again — so a repeated `Idempotency-Key` after a booking finished would book twice. Start with `id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING` (a running id returns the existing handle; verified same `first_execution_run_id`) **and** `id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE` (a closed id raises `temporalio.exceptions.WorkflowAlreadyStartedError`; verified). Both enums are in `temporalio.common`, **not** `temporalio.client`.
2. **Class-based activities** work: register bound methods (`instance.method`), call from a workflow with `workflow.execute_activity_method(Class.method, arg, ...)`. The activity's default name is the method name, so a T2 test can substitute a plain function decorated `@activity.defn(name="claim_slot")`. Verified.
3. **Non-retryable failure:** an activity raising `ApplicationError(..., non_retryable=True)` is not retried; the workflow sees `temporalio.exceptions.ActivityError` whose `.cause` is the `ApplicationError`. Verified.
4. **Sandbox:** `workflows.py` is re-imported in the sandbox. It must import `activities.py` (which pulls SQLAlchemy) inside `with workflow.unsafe.imports_passed_through():`. Plain dataclass modules can be imported normally.
5. **Import spelling rule** (slice 1 meta-test): app code imports `from temporalio import workflow` / `from temporalio import activity` only — never `from temporalio.workflow import ...`, never `import temporalio...`. `from temporalio.common import ...`, `from temporalio.exceptions import ...`, `from temporalio.client import ...` are fine.
6. **Module boundary rule 4** (`NEVER_CROSS` in `tests/meta/test_import_boundaries.py`): `service.py`, `models.py`, `schemas.py` may import **no** other domain module, allowlist or not. Every cross-module edge in this slice is declared as an explicit pair in `CROSS_MODULE_ALLOWED`; the allowlist's staleness check fails if a declared pair does not exist yet, so add each pair **in the task that creates the import**.
7. **Session semantics:** `create_session_factory` uses `expire_on_commit=False`. `session.get()` returns identity-map objects; reads that must see another transaction's write use `populate_existing=True` or a fresh session. A failed `flush()` leaves the session needing rollback.
8. **Error envelope:** `DomainError` subclasses carry `status_code`; `app/api/error_handlers.py` renders `{"error": <class name>, "detail": ...}`. Adding a subclass needs no handler change.
9. **`RPCError(message, status: RPCStatusCode, raw_grpc_status: bytes)`** — constructor signature, for fakes. A lazy client whose server is unreachable raises **`RuntimeError`** ("Failed client connect") on first use, not `RPCError` (verified in slice 1).
10. Audit follows the Week 1 order: service flushes → `audit.record(...)` → caller commits.

## File map

| File | Action | Responsibility |
| --- | --- | --- |
| `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` | modify | D-15 edge correction; id-policy note |
| `app/modules/providers/models.py` | modify | lose `SlotStatus`, `slot_status_column`, `ProviderSlot` |
| `app/modules/scheduling/models.py` | modify | gain them (verbatim) |
| `app/seed.py`, tests importing `ProviderSlot`/`SlotStatus` | modify | import from `scheduling.models` |
| `app/core/errors.py` | modify | `Unprocessable` (422), `ServiceUnavailable` (503) |
| `app/modules/patients/public.py` | create | `ensure_bookable_patient`, `patient_id_for_user` |
| `app/modules/scheduling/state.py` | create | appointment transition table |
| `app/modules/scheduling/service.py` | create | create_pending, ensure_slot_bookable, claim_slot, confirm_booking, fail_booking, get_appointment |
| `app/modules/scheduling/contracts.py` | create | `BookingRequest` dataclass shared by workflow and activities |
| `app/modules/scheduling/activities.py` | create | `SchedulingActivities` |
| `app/modules/scheduling/workflows.py` | create | `BookAppointmentWorkflow` |
| `app/modules/scheduling/booking.py` | create | id derivation, `start_booking`, `booking_in_progress` |
| `app/modules/scheduling/schemas.py`, `router.py` | create | HTTP surface |
| `app/core/temporal.py` | modify | `bind_activities` |
| `app/components.py` | modify | register workflow + activities; `build_activities` |
| `app/worker.py` | modify | build DB/audit resources for activities |
| `app/api/deps.py`, `app/api/router.py` | modify | `get_temporal`; mount router |
| `tests/meta/test_import_boundaries.py` | modify | declared pairs; router is framework-bound |
| `tests/meta/test_slot_writes.py` | create | only `scheduling/service.py` updates slot status |
| tests across T0–T3 | create | per task |
| `tests/cases/bkg-*.yaml` | create | Stop-hook cases |
| docs | modify | Task 12 |

---

### Task 0: Branch and spec correction

- [ ] **Step 1: Branch**

```bash
git switch main && git pull --ff-only && git switch -c feat/booking-workflow
```

- [ ] **Step 2: Amend the spec.** In `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §3 "Import rules", replace the bullet `The horizontal rule gains exactly one declared edge: scheduling.* → patients.public.` with:

```markdown
- The horizontal rule gains declared edges onto `patients.public` from
  `scheduling.activities` and `scheduling.router` only. Not from `scheduling.service`:
  rule 4 of `tests/meta/test_import_boundaries.py` (`NEVER_CROSS`) forbids `service.py`,
  `models.py` and `schemas.py` any cross-module import, allowlist or not. The activity
  performs the patient check and the service receives only ids. (Corrected 2026-10-28 while
  planning slice 2.)
```

In §4.1 under "Router", step 3, append: `Start with id_conflict_policy=USE_EXISTING and id_reuse_policy=REJECT_DUPLICATE: Temporal's default allows a closed id to be started again, which would book a repeated request twice.`

- [ ] **Step 3: Commit** — `docs: correct the patients.public edge and pin the workflow-id policies` (reasoned body; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` on **every** commit in this plan).

---

### Task 1: Move `ProviderSlot` into scheduling (D-9)

**Files:** `app/modules/providers/models.py`, `app/modules/scheduling/models.py`, `app/seed.py`, `tests/tiers/t0_unit/test_domain_models.py`, `tests/tiers/t3_integration/test_schema.py`, `tests/tiers/t3_integration/test_seed.py`, `tests/tiers/t0_unit/test_scheduling_models.py`

- [ ] **Step 1: Failing test** — append to `tests/tiers/t0_unit/test_scheduling_models.py`:

```python
def test_slots_belong_to_scheduling_not_providers():
    """D-9: slots are bookable inventory, mutated only by scheduling. Keeping the class in
    providers would make the paired write (appointment + slot) straddle a module boundary."""
    import app.modules.providers.models as providers_models
    import app.modules.scheduling.models as scheduling_models

    assert scheduling_models.ProviderSlot.__tablename__ == "provider_slots"
    assert hasattr(scheduling_models, "SlotStatus")
    assert not hasattr(providers_models, "ProviderSlot")
    assert not hasattr(providers_models, "SlotStatus")
```

Run: `python -m pytest tests/tiers/t0_unit/test_scheduling_models.py -q` → FAIL (`AttributeError`).

- [ ] **Step 2: Move.** Cut from `app/modules/providers/models.py` the `SlotStatus` enum, the `slot_status_column` definition with its comment, and the whole `ProviderSlot` class, **verbatim**. Paste them into `app/modules/scheduling/models.py`: `SlotStatus` and `slot_status_column` after `WaitlistStatus`/`_enum_column`, `ProviderSlot` before `Appointment`. Add the imports `scheduling/models.py` now needs (`Integer`, `UniqueConstraint` from sqlalchemy) and remove imports `providers/models.py` no longer uses (ruff will tell you). Do **not** change any column, constraint name, index or comment — the table must be byte-identical to the migration.

Update the `ProviderSlot` docstring's first line to add: `Lives in scheduling, not providers (D-9): scheduling is the only module that writes it.`

- [ ] **Step 3: Repoint imports.** `grep -rn "ProviderSlot\|SlotStatus" app tests --include=*.py` and change every `from app.modules.providers.models import ... ProviderSlot/SlotStatus` to import them from `app.modules.scheduling.models` (keep `Clinic`, `Provider`, etc. from providers). Known sites: `app/seed.py`, `tests/tiers/t0_unit/test_domain_models.py`, `tests/tiers/t3_integration/test_schema.py`, `tests/tiers/t3_integration/test_seed.py`.

- [ ] **Step 4: Verify no schema drift**

```bash
python -m pytest -m "not docker" -q
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth python -m alembic check
```

Expected: tests pass; `alembic check` prints `No new upgrade operations detected.` (Bring the test stack up first with the `test-stack` skill; run `alembic upgrade head` with the same env first if the `smarthealth` database is behind.) Then `python -m pytest -m docker -q` — all pass.

- [ ] **Step 5: Commit** — `refactor: move provider slots into scheduling, the module that books them` (body: D-9, the paired write, no schema change, alembic check clean).

---

### Task 2: Domain errors and the published patient interface

**Files:** `app/core/errors.py`, `app/modules/patients/public.py`, tests `tests/tiers/t0_unit/test_errors.py`, `tests/tiers/t3_integration/test_patients_public.py`

- [ ] **Step 1: Failing tests** — append to `tests/tiers/t0_unit/test_errors.py`:

```python
def test_unprocessable_and_unavailable_map_to_their_statuses():
    from app.core.errors import ServiceUnavailable, Unprocessable

    assert Unprocessable("x").status_code == 422
    assert ServiceUnavailable("x").status_code == 503
```

Create `tests/tiers/t3_integration/test_patients_public.py`:

```python
"""The published patient interface, against real Postgres."""

import uuid

import pytest

from app.core.errors import NotFound
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients import public
from app.modules.patients.models import Patient

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_an_existing_patient_is_bookable(db_session):
    patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe")
    db_session.add(patient)
    await db_session.flush()
    await public.ensure_bookable_patient(db_session, patient.id)


async def test_an_unknown_patient_is_not_found(db_session):
    with pytest.raises(NotFound):
        await public.ensure_bookable_patient(db_session, uuid.uuid4())


async def test_a_user_resolves_to_their_linked_patient(db_session):
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("x" * 12), role=UserRole.PATIENT,
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe", user_id=user.id
    )
    db_session.add(patient)
    await db_session.flush()

    assert await public.patient_id_for_user(db_session, user.id) == patient.id
    assert await public.patient_id_for_user(db_session, uuid.uuid4()) is None
```

Check `User`'s required columns in `app/modules/identity/models.py` before running; adjust the constructor if a required field is missing.

- [ ] **Step 2: Implement.** Append to `app/core/errors.py`:

```python
class Unprocessable(DomainError):
    """The request is well-formed but cannot be acted on, e.g. booking a past slot."""

    status_code = 422


class ServiceUnavailable(DomainError):
    """A dependency the request needs is down. Retrying later may succeed."""

    status_code = 503
```

Create `app/modules/patients/public.py`:

```python
"""The patients module's published interface (PRD D-15).

The only file another module may import from `patients`, and only from the call sites
declared in tests/meta/test_import_boundaries.py. It returns ids and raises domain
errors, never ORM objects: a caller that receives a `Patient` instance is coupled to
this module's schema, which is exactly what the boundary exists to prevent.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.patients.models import Patient


async def ensure_bookable_patient(session: AsyncSession, patient_id: uuid.UUID) -> None:
    """Raise `NotFound` unless the patient exists.

    "Bookable" is existence today -- patients carry no active flag. When eligibility grows
    rules (an inactive record, an unpaid balance), they land here and every booking path
    picks them up without changing.
    """
    found = await session.scalar(select(exists().where(Patient.id == patient_id)))
    if not found:
        raise NotFound(f"patient {patient_id} does not exist")


async def patient_id_for_user(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The patient record linked to a login, or None for a user with no patient record."""
    return await session.scalar(select(Patient.id).where(Patient.user_id == user_id))
```

- [ ] **Step 3: Run** `python -m pytest tests/tiers/t0_unit/test_errors.py tests/meta -q` (pass) and, with the stack up, `python -m pytest tests/tiers/t3_integration/test_patients_public.py -q` (3 passed).

- [ ] **Step 4: Commit** — `feat: publish the patient checks scheduling needs, and add 422/503 domain errors`.

---

### Task 3: The appointment state machine

**Files:** create `app/modules/scheduling/state.py`, `tests/tiers/t0_unit/test_scheduling_state.py`

- [ ] **Step 1: Failing test**

```python
import pytest

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S
from app.modules.scheduling.state import APPOINTMENT_TRANSITIONS, can_transition, require_transition

pytestmark = pytest.mark.unit

ALLOWED = {
    (S.PENDING, S.CONFIRMED), (S.PENDING, S.FAILED), (S.PENDING, S.CANCELLED),
    (S.CONFIRMED, S.CANCELLED), (S.CONFIRMED, S.RESCHEDULED),
    (S.CONFIRMED, S.COMPLETED), (S.CONFIRMED, S.NO_SHOW),
}


@pytest.mark.parametrize("source", list(S))
@pytest.mark.parametrize("target", list(S))
def test_every_pair_is_allowed_exactly_when_listed(source, target):
    assert can_transition(source, target) is ((source, target) in ALLOWED)


def test_confirmed_is_reachable_only_from_pending():
    """The headline invariant, stated as data."""
    assert {s for s, targets in APPOINTMENT_TRANSITIONS.items() if S.CONFIRMED in targets} == {
        S.PENDING
    }


def test_terminal_states_have_no_exit():
    for terminal in (S.CANCELLED, S.RESCHEDULED, S.COMPLETED, S.NO_SHOW, S.FAILED):
        assert not APPOINTMENT_TRANSITIONS.get(terminal)


def test_an_illegal_transition_is_a_conflict_naming_both_states():
    with pytest.raises(Conflict, match="completed.*confirmed"):
        require_transition(S.COMPLETED, S.CONFIRMED)
```

Run → FAIL (module missing).

- [ ] **Step 2: Implement** `app/modules/scheduling/state.py`:

```python
"""Appointment state transitions, as data.

Every write path asks this table before changing a status, so the lifecycle is stated
once and enumerated by tests rather than implied by scattered `if` statements. Pure: no
I/O, no session.

`confirmed` is reachable only from `pending` -- the headline invariant. The database
backs it independently (`ck_appointments_confirmed_requires_slot`), so a bug here and a
bug in the workflow would both have to happen for a slot-less confirmation to exist.
"""

from __future__ import annotations

from app.core.errors import Conflict
from app.modules.scheduling.models import AppointmentStatus as S

APPOINTMENT_TRANSITIONS: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.CONFIRMED, S.FAILED, S.CANCELLED}),
    S.CONFIRMED: frozenset({S.CANCELLED, S.RESCHEDULED, S.COMPLETED, S.NO_SHOW}),
}


def can_transition(source: S, target: S) -> bool:
    return target in APPOINTMENT_TRANSITIONS.get(source, frozenset())


def require_transition(source: S, target: S) -> None:
    """Raise `Conflict` for an illegal move. A conflict, not a validation error: the
    request was well-formed, the appointment is simply no longer in a state that allows
    it -- typically because something else moved it first."""
    if not can_transition(source, target):
        raise Conflict(
            f"an appointment cannot move from {source.value} to {target.value}"
        )
```

- [ ] **Step 3: Run** → pass. **Step 4: Commit** — `feat: state the appointment lifecycle as a transition table`.

---

### Task 4: Booking service operations

**Files:** create `app/modules/scheduling/service.py`, `tests/tiers/t3_integration/scheduling_helpers.py`, `tests/tiers/t3_integration/test_booking_service.py`, `tests/meta/test_slot_writes.py`

Every operation is **idempotent**, because a Temporal activity may run again after its transaction committed but before the result reached the server. Every slot write increments `version` (R-4) and happens in the same transaction as its appointment write (R-2).

- [ ] **Step 1: Test helpers** — `tests/tiers/t3_integration/scheduling_helpers.py`:

```python
"""Seed rows for scheduling tests. Committed, because activities and workers read them
from their own sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider
from app.modules.scheduling.models import ProviderSlot


@dataclass(frozen=True)
class Bookable:
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID


async def seed_bookable(
    session: AsyncSession, *, starts_in: timedelta = timedelta(days=1)
) -> Bookable:
    clinic = Clinic(name=f"Riverside {uuid.uuid4().hex[:8]}", timezone="UTC")
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe")
    session.add_all([clinic, provider, patient])
    await session.flush()
    start = datetime.now(UTC).replace(microsecond=0) + starts_in
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=start, ends_at=start + timedelta(minutes=30),
    )
    session.add(slot)
    await session.commit()
    return Bookable(patient.id, slot.id, provider.id, clinic.id)
```

- [ ] **Step 2: Failing integration tests** — `tests/tiers/t3_integration/test_booking_service.py`:

```python
"""Booking operations against real Postgres: idempotency, the conditional claim, the
paired write, and the rules the schema cannot enforce (R-3, R-4)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.clock import Clock
from app.core.errors import Conflict, Unprocessable
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
)
from tests.tiers.t3_integration.scheduling_helpers import seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


class _NullAudit:
    def __init__(self) -> None:
        self.events = []

    async def record(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def _slot(factory, slot_id) -> ProviderSlot:
    async with factory() as session:
        return await session.get(ProviderSlot, slot_id)


async def _appointment(factory, appointment_id) -> Appointment:
    async with factory() as session:
        return await session.get(Appointment, appointment_id)


async def _pending(factory, b, audit, appointment_id=None) -> uuid.UUID:
    appointment_id = appointment_id or uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    return appointment_id


async def test_create_pending_is_idempotent(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _pending(factory, b, audit, appointment_id)  # a retried activity

    async with factory() as session:
        rows = (await session.scalars(
            select(Appointment).where(Appointment.id == appointment_id)
        )).all()
    assert len(rows) == 1 and rows[0].status is AppointmentStatus.PENDING
    assert rows[0].provider_id == b.provider_id and rows[0].clinic_id == b.clinic_id
    assert len(audit.events) == 1, "a retry must not write a second audit entry"


async def test_a_past_slot_is_rejected(factory):
    """R-3: CHECK (starts_at > now()) is impossible in Postgres, so the service owns it."""
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    async with factory() as session:
        with pytest.raises(Unprocessable):
            await service.ensure_slot_bookable(session, slot_id=b.slot_id, now=Clock().now())


async def test_claim_increments_version_and_is_idempotent(factory):
    """R-4: version must move on every claim, or the column is decoration."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    for _ in range(2):  # the second call is a retried activity
        async with factory() as session:
            await service.claim_slot(
                session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                actor_user_id=None,
            )
            await session.commit()

    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.HELD and slot.version == 1
    assert (await _appointment(factory, appointment_id)).slot_id == b.slot_id


async def test_two_concurrent_claims_have_exactly_one_winner(factory):
    """The conditional UPDATE is the whole concurrency story (PRD D-2): zero rows
    affected is the conflict, with no window between checking and acting."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    first, second = await _pending(factory, b, audit), await _pending(factory, b, audit)

    async def claim(appointment_id):
        async with factory() as session:
            await service.claim_slot(
                session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                actor_user_id=None,
            )
            await session.commit()

    results = await asyncio.gather(claim(first), claim(second), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "NoneType"]
    assert (await _slot(factory, b.slot_id)).version == 1


async def test_confirm_books_the_slot_and_is_idempotent(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            actor_user_id=None,
        )
        await session.commit()
    for _ in range(2):
        async with factory() as session:
            await service.confirm_booking(
                session, audit, appointment_id=appointment_id, now=Clock().now(),
                actor_user_id=None,
            )
            await session.commit()

    appointment = await _appointment(factory, appointment_id)
    assert appointment.status is AppointmentStatus.CONFIRMED
    assert appointment.confirmed_at is not None
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2


async def test_fail_booking_releases_only_its_own_held_slot(factory):
    """Compensation is a paired write (R-2): appointment failed and slot free, together."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            actor_user_id=None,
        )
        await session.commit()
    for _ in range(2):  # idempotent
        async with factory() as session:
            await service.fail_booking(
                session, audit, appointment_id=appointment_id, actor_user_id=None
            )
            await session.commit()

    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.FAILED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 2


async def test_fail_booking_never_frees_a_slot_another_booking_holds(factory):
    """The loser of a race compensates; the winner's slot must stay held."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    winner, loser = await _pending(factory, b, audit), await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=winner, slot_id=b.slot_id, actor_user_id=None
        )
        await session.commit()
    async with factory() as session:
        await service.fail_booking(session, audit, appointment_id=loser, actor_user_id=None)
        await session.commit()

    assert (await _slot(factory, b.slot_id)).status is SlotStatus.HELD


async def test_fail_booking_with_no_row_is_a_no_op(factory):
    """create_pending may itself have failed; compensation must not."""
    async with factory() as session:
        await service.fail_booking(
            session, _NullAudit(), appointment_id=uuid.uuid4(), actor_user_id=None
        )
```

Run with the stack up → FAIL (`service` missing).

- [ ] **Step 3: Implement** `app/modules/scheduling/service.py`:

```python
"""Booking operations. No FastAPI, no Temporal: activities and routers both call these.

Every function is idempotent, because a Temporal activity can run again after its
transaction committed but before the server heard it finished. Each detects that it has
already done its work and returns, without a second write or a second audit entry.

Every slot write increments `version` (PRD R-4) and happens in the same transaction as
the appointment write that justifies it (R-2). tests/meta/test_slot_writes.py keeps
slot-status updates inside this file.

The caller owns the transaction: these flush, the caller commits -- the Week 1 rule
(ARCHITECTURE §2.1), which is what lets an activity wrap one call in one unit of work.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound, Unprocessable
from app.db.constraints import violated_constraint
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
)
from app.modules.scheduling.state import require_transition

ENTITY = "appointment"

#: The partial unique index that allows one live appointment per slot. Named here
#: because a claim that loses to it must become a Conflict, not a 500.
LIVE_SLOT_INDEX = "ux_appointments_live_slot"


def _snapshot(appointment: Appointment) -> dict[str, object]:
    return {
        "status": appointment.status,
        "patient_id": str(appointment.patient_id),
        "slot_id": str(appointment.slot_id) if appointment.slot_id else None,
        "provider_id": str(appointment.provider_id),
        "clinic_id": str(appointment.clinic_id),
    }


async def get_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, *, for_update: bool = False
) -> Appointment | None:
    """Always a fresh read: `populate_existing` defeats the identity map, which could
    otherwise hand back a status another transaction has since changed."""
    statement = (
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _require(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    appointment = await get_appointment(session, appointment_id, for_update=True)
    if appointment is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    return appointment


async def _audit(
    audit: AuditLog, appointment: Appointment, action: str, actor_user_id: str | None,
    before: dict[str, object] | None,
) -> None:
    await audit.record(
        AuditEvent(
            entity_type=ENTITY, entity_id=str(appointment.id), action=action,
            actor_user_id=actor_user_id, before=before, after=_snapshot(appointment),
        )
    )


async def create_pending(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    patient_id: uuid.UUID, slot_id: uuid.UUID, reason: str | None,
    actor_user_id: str | None,
) -> None:
    """Insert the `pending` row, copying provider/clinic/department from the slot.

    `ON CONFLICT (id) DO NOTHING` makes a retry harmless; only the call that actually
    inserted writes the audit entry.
    """
    slot = await session.get(ProviderSlot, slot_id)
    if slot is None:
        raise NotFound(f"slot {slot_id} does not exist")
    result = await session.execute(
        insert(Appointment)
        .values(
            id=appointment_id, patient_id=patient_id, provider_id=slot.provider_id,
            clinic_id=slot.clinic_id, department_id=slot.department_id, reason=reason,
        )
        .on_conflict_do_nothing(index_elements=[Appointment.id])
    )
    if result.rowcount == 1:
        appointment = await _require(session, appointment_id)
        await _audit(audit, appointment, "booking_requested", actor_user_id, None)


async def ensure_slot_bookable(
    session: AsyncSession, *, slot_id: uuid.UUID, now: datetime
) -> None:
    """R-3: a slot that has started cannot be booked. Postgres cannot enforce this --
    CHECK conditions must be immutable, and now() is not."""
    slot = await session.get(ProviderSlot, slot_id)
    if slot is None:
        raise NotFound(f"slot {slot_id} does not exist")
    if slot.starts_at <= now:
        raise Unprocessable(f"slot {slot_id} started at {slot.starts_at.isoformat()}")


async def claim_slot(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    slot_id: uuid.UUID, actor_user_id: str | None,
) -> None:
    """The conditional claim (PRD D-2). Zero rows updated *is* the conflict.

    The partial unique index `ux_appointments_live_slot` is the second gate: if the
    UPDATE were ever wrong, attaching `slot_id` to a second live appointment still fails.
    """
    appointment = await _require(session, appointment_id)
    if appointment.slot_id == slot_id:
        return  # already claimed by this booking: a retried activity
    if appointment.status is not AppointmentStatus.PENDING:
        raise Conflict(f"appointment {appointment_id} is {appointment.status.value}")
    before = _snapshot(appointment)

    claimed = await session.scalar(
        update(ProviderSlot)
        .where(ProviderSlot.id == slot_id, ProviderSlot.status == SlotStatus.FREE)
        .values(status=SlotStatus.HELD, version=ProviderSlot.version + 1)
        .returning(ProviderSlot.id)
    )
    if claimed is None:
        raise Conflict(f"slot {slot_id} is no longer free")

    appointment.slot_id = slot_id
    try:
        await session.flush()
    except IntegrityError as exc:
        if violated_constraint(exc) == LIVE_SLOT_INDEX:
            raise Conflict(f"slot {slot_id} is held by another appointment") from exc
        raise
    await _audit(audit, appointment, "slot_claimed", actor_user_id, before)


async def confirm_booking(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    now: datetime, actor_user_id: str | None,
) -> None:
    """pending -> confirmed and held -> booked, in one transaction."""
    appointment = await _require(session, appointment_id)
    if appointment.status is AppointmentStatus.CONFIRMED:
        return
    require_transition(appointment.status, AppointmentStatus.CONFIRMED)
    if appointment.slot_id is None:
        raise Conflict(f"appointment {appointment_id} holds no slot")
    before = _snapshot(appointment)

    booked = await session.scalar(
        update(ProviderSlot)
        .where(ProviderSlot.id == appointment.slot_id, ProviderSlot.status == SlotStatus.HELD)
        .values(status=SlotStatus.BOOKED, version=ProviderSlot.version + 1)
        .returning(ProviderSlot.id)
    )
    if booked is None:
        raise Conflict(f"slot {appointment.slot_id} is not held")
    appointment.status = AppointmentStatus.CONFIRMED
    appointment.confirmed_at = now
    await session.flush()
    await _audit(audit, appointment, "confirmed", actor_user_id, before)


async def fail_booking(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    actor_user_id: str | None,
) -> None:
    """Compensation: appointment -> failed, and its held slot -> free, together.

    A no-op when there is nothing to undo: no row (create_pending never ran), already
    failed (a retry), or no longer pending (something else, e.g. a cancellation, owns the
    appointment now and has already dealt with the slot). The slot is released only if
    *this* appointment holds it, so the loser of a race never frees the winner's slot.
    """
    appointment = await get_appointment(session, appointment_id, for_update=True)
    if appointment is None or appointment.status is not AppointmentStatus.PENDING:
        return
    before = _snapshot(appointment)
    if appointment.slot_id is not None:
        await session.execute(
            update(ProviderSlot)
            .where(
                ProviderSlot.id == appointment.slot_id,
                ProviderSlot.status == SlotStatus.HELD,
            )
            .values(status=SlotStatus.FREE, version=ProviderSlot.version + 1)
        )
    require_transition(appointment.status, AppointmentStatus.FAILED)
    appointment.status = AppointmentStatus.FAILED
    await session.flush()
    await _audit(audit, appointment, "booking_failed", actor_user_id, before)
```

**Check before trusting the "never frees the winner's slot" property:** `fail_booking` for the *loser* — whose `slot_id` is still `None` because its claim failed — never touches the slot. That is what `test_fail_booking_never_frees_a_slot_another_booking_holds` proves. If a loser could ever have `slot_id` set while the winner holds the slot, the `WHERE status == HELD` clause would free the winner's slot; the partial unique index makes that state impossible. Say so in your report if you find otherwise.

Confirm `violated_constraint` exists with that signature in `app/db/constraints.py` (it is used by `patients/service.py`).

- [ ] **Step 4: Slot-write guard** — `tests/meta/test_slot_writes.py`:

```python
"""Only scheduling/service.py may update a slot's status.

R-2: a slot's status and its appointment's status are two independently writable facts,
and the database cannot keep them in agreement. The service keeps every slot update
inside the transaction that changes the appointment. A second place that issues
`update(ProviderSlot)` is a second place that can forget the other half.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
ALLOWED = APP_ROOT / "modules" / "scheduling" / "service.py"


def _updates_provider_slot(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "update"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "ProviderSlot"
        for node in ast.walk(tree)
    )


def test_only_the_scheduling_service_updates_slots():
    offenders = [
        str(path.relative_to(APP_ROOT.parent))
        for path in APP_ROOT.rglob("*.py")
        if path != ALLOWED and _updates_provider_slot(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"slot status updated outside scheduling/service.py: {offenders}"


def test_the_guard_sees_the_service():
    assert _updates_provider_slot(ast.parse(ALLOWED.read_text(encoding="utf-8")))
```

- [ ] **Step 5: Run** `python -m pytest tests/meta -q` and `python -m pytest tests/tiers/t3_integration/test_booking_service.py -q` → all pass.

- [ ] **Step 6: Commit** — `feat: add idempotent booking operations with the conditional slot claim` (body: idempotency rationale, R-2/R-3/R-4, the slot-write guard).

---

### Task 5: Shared contract and activities

**Files:** create `app/modules/scheduling/contracts.py`, `app/modules/scheduling/activities.py`; modify `app/core/temporal.py`, `app/components.py`, `tests/meta/test_import_boundaries.py`, `tests/meta/test_temporal_registry.py`; test `tests/tiers/t0_unit/test_bind_activities.py`

- [ ] **Step 1: Contract** — `app/modules/scheduling/contracts.py`:

```python
"""Values passed between the booking workflow and its activities.

Plain dataclasses of strings: Temporal serialises them as JSON, and a type the default
converter cannot encode (uuid.UUID) would fail at runtime, not import time. Pure, so the
workflow sandbox imports it directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookingRequest:
    appointment_id: str
    patient_id: str
    slot_id: str
    actor_user_id: str | None
    reason: str | None = None
```

- [ ] **Step 2: Failing unit test for binding** — `tests/tiers/t0_unit/test_bind_activities.py`:

```python
import pytest

from app.core.temporal import bind_activities

pytestmark = pytest.mark.unit


class Owner:
    async def act(self) -> str:
        return "bound"


async def module_level() -> str:
    return "free"


async def test_methods_bind_to_their_owner_and_functions_pass_through():
    owner = Owner()
    bound = bind_activities([Owner.act, module_level], [owner])
    assert bound[0].__self__ is owner
    assert bound[1] is module_level


def test_a_method_with_no_instance_fails_loudly():
    with pytest.raises(LookupError, match="Owner"):
        bind_activities([Owner.act], [])
```

Run → FAIL (`bind_activities` missing).

- [ ] **Step 3: Implement `bind_activities`** — append to `app/core/temporal.py`:

```python
def bind_activities(
    functions: Sequence[Callable[..., Any]], instances: Sequence[object]
) -> list[Callable[..., Any]]:
    """Turn registered activity functions into what a Worker runs.

    The registry holds plain functions -- discoverable, and comparable with the source
    tree by the registry meta-test. Activities that need a database are methods of a
    class constructed with its resources at worker start-up, so each method is bound to
    the instance of its owning class here. A method with no instance is a wiring bug and
    raises, rather than registering an activity that fails on its first call.
    """
    by_class = {type(instance).__qualname__: instance for instance in instances}
    bound: list[Callable[..., Any]] = []
    for function in functions:
        owner, _, name = function.__qualname__.rpartition(".")
        if not owner:
            bound.append(function)
            continue
        if owner not in by_class:
            raise LookupError(f"no instance of {owner} to bind activity {name} to")
        bound.append(getattr(by_class[owner], name))
    return bound
```

Run → pass.

- [ ] **Step 4: Activities** — `app/modules/scheduling/activities.py`:

```python
"""Booking activities: one service call, one transaction, per activity.

Each activity opens its own session and commits, so a retried activity re-runs exactly
one unit of work against the idempotent service functions. Domain errors become
non-retryable `ApplicationError`s -- a lost slot race or a past slot will not change on
retry, and retrying it only delays compensation. Anything else (a dropped connection)
propagates and Temporal retries it.

Billing and reminders are stand-ins until Week 3 (spec §1): the workflow has its final
shape now, and Week 3 replaces these bodies, not the workflow.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.errors import DomainError
from app.modules.patients import public as patients
from app.modules.scheduling import service
from app.modules.scheduling.contracts import BookingRequest

logger = logging.getLogger(__name__)


class SchedulingActivities:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._clock = clock

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except DomainError as exc:
                await session.rollback()
                raise ApplicationError(
                    exc.detail, type=type(exc).__name__, non_retryable=True
                ) from exc
            except BaseException:
                await session.rollback()
                raise

    @activity.defn
    async def create_pending(self, request: BookingRequest) -> None:
        async with self._unit_of_work() as session:
            await service.create_pending(
                session, self._audit, appointment_id=uuid.UUID(request.appointment_id),
                patient_id=uuid.UUID(request.patient_id), slot_id=uuid.UUID(request.slot_id),
                reason=request.reason, actor_user_id=request.actor_user_id,
            )

    @activity.defn
    async def validate_booking(self, request: BookingRequest) -> None:
        async with self._unit_of_work() as session:
            await patients.ensure_bookable_patient(session, uuid.UUID(request.patient_id))
            await service.ensure_slot_bookable(
                session, slot_id=uuid.UUID(request.slot_id), now=self._clock.now()
            )

    @activity.defn
    async def claim_slot(self, request: BookingRequest) -> None:
        async with self._unit_of_work() as session:
            await service.claim_slot(
                session, self._audit, appointment_id=uuid.UUID(request.appointment_id),
                slot_id=uuid.UUID(request.slot_id), actor_user_id=request.actor_user_id,
            )

    @activity.defn
    async def billing_precheck(self, request: BookingRequest) -> None:
        """Stand-in: approves. Week 3 replaces the body; a decline must raise
        `ApplicationError(non_retryable=True)` so the workflow compensates."""
        logger.info("billing pre-check approved (stand-in)",
                    extra={"appointment_id": request.appointment_id})

    @activity.defn
    async def schedule_reminders(self, request: BookingRequest) -> None:
        """Stand-in: logs. Week 3 enqueues delivery through Celery."""
        logger.info("reminders scheduled (stand-in)",
                    extra={"appointment_id": request.appointment_id})

    @activity.defn
    async def confirm(self, request: BookingRequest) -> None:
        async with self._unit_of_work() as session:
            await service.confirm_booking(
                session, self._audit, appointment_id=uuid.UUID(request.appointment_id),
                now=self._clock.now(), actor_user_id=request.actor_user_id,
            )

    @activity.defn
    async def fail_booking(self, request: BookingRequest) -> None:
        async with self._unit_of_work() as session:
            await service.fail_booking(
                session, self._audit, appointment_id=uuid.UUID(request.appointment_id),
                actor_user_id=request.actor_user_id,
            )
```

- [ ] **Step 5: Declare the edge.** In `tests/meta/test_import_boundaries.py`, add to `CROSS_MODULE_ALLOWED`:

```python
        # D-15: the booking activity checks patient eligibility through the published
        # interface. Not from scheduling.service -- rule 4 forbids that outright.
        ("app.modules.scheduling.activities", "app.modules.patients.public"),
```

Update the comment above `CROSS_MODULE_ALLOWED` ("Every entry here is auth at the HTTP edge") to say entries are auth at the HTTP edge **or** a module's published interface.

- [ ] **Step 6: Register.** Rewrite `app/components.py`:

```python
"""The one place Temporal components are registered.

Not in the workflow modules themselves: Temporal re-imports a workflow's module inside
its sandbox, so a registration call there would run twice. This module is imported by
the worker and the tests, never by the sandbox.

Activities are registered as plain functions (discoverable by the registry meta-test)
and bound to instances carrying their resources by `build_activities` at worker start.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import ACTIVITIES, WORKFLOWS, bind_activities
from app.core.temporal_canary import CanaryWorkflow
from app.modules.scheduling.activities import SchedulingActivities

_registered = False

_SCHEDULING_ACTIVITIES = (
    SchedulingActivities.create_pending,
    SchedulingActivities.validate_booking,
    SchedulingActivities.claim_slot,
    SchedulingActivities.billing_precheck,
    SchedulingActivities.schedule_reminders,
    SchedulingActivities.confirm,
    SchedulingActivities.fail_booking,
)


def register_all() -> None:
    """Populate the registries. Idempotent."""
    global _registered
    if _registered:
        return
    WORKFLOWS.register(CanaryWorkflow.__name__, CanaryWorkflow)
    for function in _SCHEDULING_ACTIVITIES:
        ACTIVITIES.register(function.__name__, function)
    _registered = True


def build_activities(
    session_factory: async_sessionmaker[AsyncSession], audit: AuditLog, clock: Clock
) -> list[Callable[..., Any]]:
    """Every registered activity, bound to instances holding the worker's resources."""
    register_all()
    instances = [SchedulingActivities(session_factory, audit, clock)]
    return bind_activities(list(ACTIVITIES), instances)
```

The workflow registration is added in Task 6.

- [ ] **Step 7: Run** `python -m pytest tests/meta tests/tiers/t0_unit -q` → pass. `test_every_activity_definition_is_registered` now compares 7 discovered with 7 registered.

- [ ] **Step 8: Commit** — `feat: add booking activities, each one service call in one transaction`.

---

### Task 6: The booking workflow

**Files:** create `app/modules/scheduling/workflows.py`, `tests/tiers/t2_workflow/test_booking_workflow.py`; modify `app/components.py`

- [ ] **Step 1: Failing T2 tests** — `tests/tiers/t2_workflow/test_booking_workflow.py`:

```python
"""The headline invariant at the workflow layer: confirmed only after every step, and
any failure compensates. Activities are replaced by recording fakes with the same names;
the workflow under test is the real one, executing in the real sandbox."""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

STEPS = [
    "create_pending", "validate_booking", "claim_slot",
    "billing_precheck", "schedule_reminders", "confirm",
]


def _fakes(calls: list[str], *, fail: str | None = None, transient: int = 0):
    """Activity fakes. `fail` raises a permanent error at that step; `transient` makes
    claim_slot raise a retryable error that many times first."""
    remaining = {"transient": transient}

    def make(name: str):
        @activity.defn(name=name)
        async def fake(request: BookingRequest) -> None:
            calls.append(name)
            if name == "claim_slot" and remaining["transient"]:
                remaining["transient"] -= 1
                raise RuntimeError("connection reset")
            if name == fail:
                raise ApplicationError(f"{name} refused", type="Conflict", non_retryable=True)

        return fake

    return [make(name) for name in [*STEPS, "fail_booking"]]


def _request() -> BookingRequest:
    return BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )


async def _run(env, activities) -> str:
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        env.client, queue, workflows=[BookAppointmentWorkflow], activities=activities
    ):
        return await env.client.execute_workflow(
            BookAppointmentWorkflow.run, _request(),
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )


async def test_confirms_only_after_every_step_in_order(temporal_env):
    calls: list[str] = []
    assert await _run(temporal_env, _fakes(calls)) == "confirmed"
    assert calls == STEPS


@pytest.mark.parametrize("failing", STEPS[1:])
async def test_any_failing_step_compensates_and_never_confirms(temporal_env, failing):
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(temporal_env, _fakes(calls, fail=failing))
    assert calls[-1] == "fail_booking"
    assert calls.count(failing) == 1, "a permanent failure must not be retried"
    if failing != "confirm":
        assert "confirm" not in calls


async def test_a_failed_create_has_nothing_to_compensate(temporal_env):
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(temporal_env, _fakes(calls, fail="create_pending"))
    assert calls == ["create_pending"]


async def test_a_transient_error_is_retried_not_compensated(temporal_env):
    calls: list[str] = []
    assert await _run(temporal_env, _fakes(calls, transient=2)) == "confirmed"
    assert calls.count("claim_slot") == 3
    assert "fail_booking" not in calls
```

Run → FAIL (module missing).

- [ ] **Step 2: Implement** `app/modules/scheduling/workflows.py`:

```python
"""Booking as a durable workflow (spec §4.1, ARCHITECTURE §3.2).

`confirmed` is the last write, so it is unreachable unless every earlier step
succeeded. Any failure after the row exists runs `fail_booking`, which returns a held
slot and marks the appointment failed in one transaction -- partial failure leaves no
orphaned slot and no half-confirmed appointment (PART-A-NFR-2).

Deterministic by construction: no I/O, no clock, no randomness here; all of that lives
in the activities. This module is re-imported inside Temporal's sandbox, so the
activities module (which pulls in SQLAlchemy) is imported through the passthrough.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from app.modules.scheduling.contracts import BookingRequest

with workflow.unsafe.imports_passed_through():
    from app.modules.scheduling.activities import SchedulingActivities

#: A step: retried with backoff for transient failure, bounded so a dead database
#: fails the booking (and compensates) instead of hanging it.
STEP = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(
        initial_interval=timedelta(seconds=1), maximum_attempts=5
    ),
}

#: Compensation is retried without limit: giving up would leave exactly the held slot
#: and pending appointment this workflow exists to prevent.
COMPENSATION = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(
        initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1)
    ),
}


@workflow.defn(name="BookAppointment")
class BookAppointmentWorkflow:
    @workflow.run
    async def run(self, request: BookingRequest) -> str:
        await workflow.execute_activity_method(
            SchedulingActivities.create_pending, request, **STEP
        )
        try:
            for step in (
                SchedulingActivities.validate_booking,
                SchedulingActivities.claim_slot,
                SchedulingActivities.billing_precheck,
                SchedulingActivities.schedule_reminders,
                SchedulingActivities.confirm,
            ):
                await workflow.execute_activity_method(step, request, **STEP)
        except ActivityError as err:
            await workflow.execute_activity_method(
                SchedulingActivities.fail_booking, request, **COMPENSATION
            )
            raise ApplicationError(
                f"booking {request.appointment_id} failed: {err.cause}",
                type="BookingFailed", non_retryable=True,
            ) from err
        return "confirmed"
```

- [ ] **Step 3: Register** — in `app/components.py` import `from app.modules.scheduling.workflows import BookAppointmentWorkflow` and add `WORKFLOWS.register(BookAppointmentWorkflow.__name__, BookAppointmentWorkflow)` after the canary line.

- [ ] **Step 4: Run** `python -m pytest tests/tiers/t2_workflow tests/meta -q` → pass, including `test_registered_workflows_pass_the_sandbox[BookAppointmentWorkflow]`.

If `test_a_transient_error_is_retried_not_compensated` is slow because retry backoff is not time-skipped, report the duration; do not shorten `STEP`'s policy for the test's sake.

- [ ] **Step 5: Commit** — `feat: add the booking workflow, confirming last and compensating on failure`.

---

### Task 7: Starting the workflow — ids and policies

**Files:** create `app/modules/scheduling/booking.py`, `tests/tiers/t0_unit/test_booking_ids.py`, `tests/tiers/t2_workflow/test_booking_start.py`

- [ ] **Step 1: Failing tests** — `tests/tiers/t0_unit/test_booking_ids.py`:

```python
import uuid

import pytest

from app.modules.scheduling.booking import appointment_id_for, booking_workflow_id

pytestmark = pytest.mark.unit


def test_the_same_caller_and_key_always_name_the_same_appointment():
    assert appointment_id_for("user-1", "k") == appointment_id_for("user-1", "k")


def test_different_callers_cannot_collide_on_one_key():
    assert appointment_id_for("user-1", "k") != appointment_id_for("user-2", "k")


def test_no_key_means_a_new_booking_every_time():
    assert appointment_id_for("user-1", None) != appointment_id_for("user-1", None)


def test_the_workflow_id_is_derived_from_the_appointment():
    appointment_id = uuid.uuid4()
    assert booking_workflow_id(appointment_id) == f"booking-{appointment_id}"
```

`tests/tiers/t2_workflow/test_booking_start.py`:

```python
"""Duplicate prevention (D-11): a repeated start maps to the same booking, whether the
first is still running or has already finished."""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity

from app.core.temporal import build_worker
from app.modules.scheduling.booking import start_booking
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

NAMES = [
    "create_pending", "validate_booking", "claim_slot", "billing_precheck",
    "schedule_reminders", "confirm", "fail_booking",
]


def _counting_fakes(counter: dict[str, int]):
    def make(name):
        @activity.defn(name=name)
        async def fake(request: BookingRequest) -> None:
            counter[name] = counter.get(name, 0) + 1

        return fake

    return [make(n) for n in NAMES]


async def test_a_repeat_after_completion_does_not_book_again(temporal_env):
    counter: dict[str, int] = {}
    queue = f"t2-start-{uuid.uuid4().hex[:8]}"
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    async with build_worker(
        temporal_env.client, queue, workflows=[BookAppointmentWorkflow],
        activities=_counting_fakes(counter),
    ):
        first = await start_booking(temporal_env.client, request, task_queue=queue)
        await temporal_env.client.get_workflow_handle(first).result()
        second = await start_booking(temporal_env.client, request, task_queue=queue)

    assert first == second
    assert counter["create_pending"] == 1
```

Run → FAIL.

- [ ] **Step 2: Implement** `app/modules/scheduling/booking.py`:

```python
"""Starting a booking: id derivation and the workflow-start policy (D-10, D-11).

Framework-free so the router stays thin and the policy is testable against the
time-skipping server.
"""

from __future__ import annotations

import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.core.errors import ServiceUnavailable
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

#: Fixed namespace for idempotency-key ids. Never change it: every derived appointment
#: id would change with it, and a client's retry would become a second booking.
IDEMPOTENCY_NAMESPACE = uuid.UUID("6f1d3a52-6a7e-4f5b-9c51-0b1f7d0e2a11")


def appointment_id_for(caller: str, idempotency_key: str | None) -> uuid.UUID:
    """With a key, the same caller and key always name the same appointment; the caller
    is part of the name so two users cannot collide on one key. Without, a new id."""
    if idempotency_key is None:
        return uuid.uuid4()
    return uuid.uuid5(IDEMPOTENCY_NAMESPACE, f"{caller}:{idempotency_key}")


def booking_workflow_id(appointment_id: uuid.UUID | str) -> str:
    return f"booking-{appointment_id}"


async def start_booking(client: Client, request: BookingRequest, *, task_queue: str) -> str:
    """Start the booking workflow, or answer for the one already started.

    USE_EXISTING returns the running workflow's handle for a repeat while it runs.
    REJECT_DUPLICATE makes a repeat after it closed raise instead of starting a second
    booking -- Temporal's default would allow it. Either way the caller gets the same
    workflow id back, which is the answer it got the first time.
    """
    workflow_id = booking_workflow_id(request.appointment_id)
    try:
        await client.start_workflow(
            BookAppointmentWorkflow.run, request, id=workflow_id, task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
    except (RPCError, RuntimeError) as exc:
        # RuntimeError: a lazy client raises it ("Failed client connect") when the
        # server is unreachable on first use -- verified on temporalio 1.34.
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    return workflow_id


async def booking_in_progress(client: Client, appointment_id: uuid.UUID) -> bool:
    """True if Temporal knows a booking workflow for this appointment. Answers the
    milliseconds between the 202 and the workflow's first activity inserting the row."""
    try:
        await client.get_workflow_handle(booking_workflow_id(appointment_id)).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    except RuntimeError as exc:
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    return True
```

- [ ] **Step 3: Run** `python -m pytest tests/tiers/t0_unit/test_booking_ids.py tests/tiers/t2_workflow -q` → pass.

- [ ] **Step 4: Commit** — `feat: derive booking ids and start workflows so a repeat never books twice`.

---

### Task 8: HTTP surface

**Files:** create `app/modules/scheduling/schemas.py`, `app/modules/scheduling/router.py`, `tests/tiers/t1_contract/test_appointments_contract.py`; modify `app/api/deps.py`, `app/api/router.py`, `tests/meta/test_import_boundaries.py`

- [ ] **Step 1: Schemas** — `app/modules/scheduling/schemas.py`:

```python
"""Appointment request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.scheduling.models import AppointmentStatus


class BookingCreate(BaseModel):
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=2000)


class BookingAccepted(BaseModel):
    """202: the booking is pending. Poll GET /appointments/{appointment_id}."""

    appointment_id: uuid.UUID
    status: AppointmentStatus = AppointmentStatus.PENDING


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: AppointmentStatus
    #: True only between the 202 and the workflow's first activity writing the row.
    in_progress: bool = False
    patient_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None
    clinic_id: uuid.UUID | None = None
    slot_id: uuid.UUID | None = None
    reason: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime | None = None
```

- [ ] **Step 2: Dependency** — append to `app/api/deps.py` (and add `from temporalio.client import Client`):

```python
def get_temporal(request: Request) -> Client:
    """FastAPI dependency: the app's lazy Temporal client. Overridden in contract tests,
    which must never reach a real server."""
    return request.app.state.temporal
```

- [ ] **Step 3: Failing contract tests** — `tests/tiers/t1_contract/test_appointments_contract.py`:

```python
"""The booking endpoint's HTTP contract, with Temporal faked. No database is touched:
these paths authorise by role before any query runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from temporalio.service import RPCError, RPCStatusCode

from app.api.deps import get_temporal
from app.main import app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()
CALLER = "11111111-1111-1111-1111-111111111111"
BODY = {"patient_id": str(uuid.uuid4()), "slot_id": str(uuid.uuid4())}


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=CALLER, role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


class FakeTemporal:
    def __init__(self, error: Exception | None = None) -> None:
        self.starts: list[dict] = []
        self._error = error

    async def start_workflow(self, *args, **kwargs):
        if self._error:
            raise self._error
        self.starts.append(kwargs)


@pytest.fixture
def temporal():
    fake = FakeTemporal()
    app.dependency_overrides[get_temporal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_temporal, None)


async def test_booking_requires_a_token(api_client, temporal):
    assert (await api_client.post("/appointments", json=BODY)).status_code == 401


async def test_a_provider_may_not_book(api_client, temporal):
    response = await api_client.post("/appointments", json=BODY, headers=auth(UserRole.PROVIDER))
    assert response.status_code == 403


async def test_front_desk_booking_is_accepted_not_created(api_client, temporal):
    """202, not 201: the appointment is pending until the workflow confirms it."""
    response = await api_client.post(
        "/appointments", json=BODY, headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert temporal.starts[0]["id"] == f"booking-{response.json()['appointment_id']}"


async def test_the_same_idempotency_key_names_the_same_appointment(api_client, temporal):
    headers = {**auth(UserRole.FRONT_DESK), "Idempotency-Key": "abc"}
    first = await api_client.post("/appointments", json=BODY, headers=headers)
    second = await api_client.post("/appointments", json=BODY, headers=headers)
    assert first.json()["appointment_id"] == second.json()["appointment_id"]


async def test_temporal_down_is_a_503_in_the_standard_envelope(api_client):
    fake = FakeTemporal(RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    app.dependency_overrides[get_temporal] = lambda: fake
    try:
        response = await api_client.post(
            "/appointments", json=BODY, headers=auth(UserRole.ADMIN)
        )
    finally:
        app.dependency_overrides.pop(get_temporal, None)
    assert response.status_code == 503
    assert response.json()["error"] == "ServiceUnavailable"


async def test_reading_requires_a_token(api_client):
    assert (await api_client.get(f"/appointments/{uuid.uuid4()}")).status_code == 401
```

Run → FAIL (404s: no router).

- [ ] **Step 4: Router** — `app/modules/scheduling/router.py`:

```python
"""Appointment routes.

Booking answers 202, not 201: the appointment is `pending` until the workflow confirms
it, and a 201 would claim a resource in a state it is not in yet (ARCHITECTURE §3.2).
A patient books and reads only their own appointments; staff act for anyone.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.api.deps import get_session, get_temporal
from app.core.errors import NotFound, PermissionDenied
from app.core.temporal import TASK_QUEUE
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.patients import public as patients
from app.modules.scheduling import service
from app.modules.scheduling.booking import (
    appointment_id_for,
    booking_in_progress,
    start_booking,
)
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.models import AppointmentStatus
from app.modules.scheduling.schemas import AppointmentRead, BookingAccepted, BookingCreate

router = APIRouter(prefix="/appointments", tags=["appointments"])

BOOKERS = (UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
READERS = (UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN)


async def _own_patient_id(session: AsyncSession, claims: TokenClaims) -> uuid.UUID | None:
    return await patients.patient_id_for_user(session, uuid.UUID(claims.subject))


@router.post("", response_model=BookingAccepted, status_code=status.HTTP_202_ACCEPTED)
async def book_appointment(
    body: BookingCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
    session: AsyncSession = Depends(get_session),
    temporal: Client = Depends(get_temporal),
    claims: TokenClaims = Depends(require_role(*BOOKERS)),
) -> BookingAccepted:
    if claims.role is UserRole.PATIENT and await _own_patient_id(session, claims) != body.patient_id:
        raise PermissionDenied("a patient may only book for themselves")

    appointment_id = appointment_id_for(claims.subject, idempotency_key)
    await start_booking(
        temporal,
        BookingRequest(
            appointment_id=str(appointment_id), patient_id=str(body.patient_id),
            slot_id=str(body.slot_id), actor_user_id=claims.subject, reason=body.reason,
        ),
        task_queue=request.app.state.settings.task_queue(TASK_QUEUE),
    )
    return BookingAccepted(appointment_id=appointment_id)


@router.get("/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    temporal: Client = Depends(get_temporal),
    claims: TokenClaims = Depends(require_role(*READERS)),
) -> AppointmentRead:
    appointment = await service.get_appointment(session, appointment_id)
    if appointment is None:
        if await booking_in_progress(temporal, appointment_id):
            return AppointmentRead(
                id=appointment_id, status=AppointmentStatus.PENDING, in_progress=True
            )
        raise NotFound(f"appointment {appointment_id} does not exist")
    # 404 rather than 403 for someone else's appointment: a patient must not be able to
    # probe which appointment ids exist.
    if claims.role is UserRole.PATIENT and await _own_patient_id(session, claims) != appointment.patient_id:
        raise NotFound(f"appointment {appointment_id} does not exist")
    return AppointmentRead.model_validate(appointment)
```

Wrap the two long `if` lines to ≤100 chars.

- [ ] **Step 5: Mount and declare.** In `app/api/router.py` import `from app.modules.scheduling import router as scheduling_router` and `api_router.include_router(scheduling_router.router)`. In `tests/meta/test_import_boundaries.py`:
  - add `"app.modules.scheduling.router"` to `FRAMEWORK_BOUND`;
  - add to `CROSS_MODULE_ALLOWED`:

```python
        ("app.modules.scheduling.router", "app.modules.identity.deps"),
        ("app.modules.scheduling.router", "app.modules.identity.models"),
        ("app.modules.scheduling.router", "app.modules.identity.security"),
        ("app.modules.scheduling.router", "app.modules.patients.public"),
```

- [ ] **Step 6: Run** `python -m pytest -m "not docker" -q` → all pass.

- [ ] **Step 7: Commit** — `feat: expose booking over HTTP as 202 plus a status read`.

---

### Task 9: Worker wiring and the end-to-end round trip

**Files:** modify `app/worker.py`, `tests/tiers/t3_integration/conftest.py`; create `tests/tiers/t3_integration/test_booking_round_trip.py`

- [ ] **Step 1: Worker resources.** In `app/worker.py` `_run`, replace `register_all()` and the `build_worker(...)` call so the worker builds real activities:

```python
    engine = create_engine(settings)
    mongo = create_mongo_client(settings)
    audit = AuditLog(get_audit_collection(mongo, settings), Clock())
    activities = build_activities(create_session_factory(engine), audit, Clock())
    ...
    worker = build_worker(client, queue, workflows=list(WORKFLOWS), activities=activities)
```

Imports: `from app.components import build_activities, register_all` (keep `register_all()` before the log line so `WORKFLOWS.names()` is populated), `from app.core.audit import AuditLog`, `from app.core.clock import Clock`, `from app.db.engine import create_engine`, `from app.db.mongo import create_mongo_client, get_audit_collection`, `from app.db.session import create_session_factory`. After `await running`, dispose: `await engine.dispose()` and `await mongo.close()` in a `try/finally` around the run. Keep the existing signal handling intact.

Also add `SMARTHEALTH_REDIS_HOST`-free env to compose? No — the `worker` compose service already has Postgres and Mongo env (slice 1). Verify by reading it.

- [ ] **Step 2: Point the T3 `api` fixture at Temporal.** In `tests/tiers/t3_integration/conftest.py` `api` fixture's env dict, add:

```python
        "SMARTHEALTH_TEMPORAL_HOST": db_settings.temporal_host,
        "SMARTHEALTH_TEMPORAL_PORT": str(db_settings.temporal_port),
        "SMARTHEALTH_RESOURCE_PREFIX": db_settings.resource_prefix,
```

and after the existing `live.*` assertions: `assert live.task_queue("x") == db_settings.task_queue("x")`.

- [ ] **Step 3: Round-trip tests** — `tests/tiers/t3_integration/test_booking_round_trip.py`:

```python
"""Booking end to end: HTTP -> real Temporal -> real worker -> real Postgres and Mongo.

The worker runs in-process on this run's prefixed task queue, so the test needs no
container beyond the compose test stack.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowFailureError

from app.components import build_activities, register_all
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import TASK_QUEUE, WORKFLOWS, build_worker, connect_client
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.scheduling.booking import booking_workflow_id
from app.modules.scheduling.models import ProviderSlot, SlotStatus
from tests.tiers.t3_integration.scheduling_helpers import seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def booking_stack(db_settings):
    """A worker with real activities, plus a session factory and Temporal client.

    Not named `stack`: that is the session-scoped compose fixture `db_settings` depends
    on, and shadowing it with a function-scoped one is a pytest ScopeMismatch."""
    register_all()
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(factory, audit, Clock()),
    )
    async with worker:
        yield factory, client
    await engine.dispose()
    await mongo.close()


async def _finish(client, appointment_id: str):
    return await client.get_workflow_handle(booking_workflow_id(appointment_id)).result()


async def test_a_booking_is_confirmed_end_to_end(api, token_for, booking_stack, audit_documents):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = token_for(UserRole.FRONT_DESK)

    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=headers,
    )
    assert accepted.status_code == 202
    appointment_id = accepted.json()["appointment_id"]
    assert await _finish(client, appointment_id) == "confirmed"

    read = await api.get(f"/appointments/{appointment_id}", headers=headers)
    assert read.status_code == 200
    assert read.json()["status"] == "confirmed" and read.json()["slot_id"] == str(b.slot_id)
    async with factory() as session:
        slot = await session.get(ProviderSlot, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2

    cursor = audit_documents.find({"entity_id": appointment_id}).sort("at", 1)
    actions = [d["action"] async for d in cursor]
    assert actions == ["booking_requested", "slot_claimed", "confirmed"]


async def test_a_repeated_request_books_once(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = {**token_for(UserRole.FRONT_DESK), "Idempotency-Key": uuid.uuid4().hex}
    body = {"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)}

    first = await api.post("/appointments", json=body, headers=headers)
    await _finish(client, first.json()["appointment_id"])
    second = await api.post("/appointments", json=body, headers=headers)

    assert second.status_code == 202
    assert second.json()["appointment_id"] == first.json()["appointment_id"]


async def test_a_past_slot_fails_and_leaves_the_slot_free(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    headers = token_for(UserRole.FRONT_DESK)

    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=headers,
    )
    appointment_id = accepted.json()["appointment_id"]
    with pytest.raises(WorkflowFailureError):
        await _finish(client, appointment_id)

    read = await api.get(f"/appointments/{appointment_id}", headers=headers)
    assert read.json()["status"] == "failed"
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.FREE


async def test_two_bookings_for_one_slot_confirm_exactly_one(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = token_for(UserRole.FRONT_DESK)
    body = {"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)}

    accepted = await asyncio.gather(
        api.post("/appointments", json=body, headers=headers),
        api.post("/appointments", json=body, headers=headers),
    )
    ids = [r.json()["appointment_id"] for r in accepted]
    outcomes = await asyncio.gather(*(_finish(client, i) for i in ids), return_exceptions=True)

    assert sorted(type(o).__name__ for o in outcomes) == ["WorkflowFailureError", "str"]
    statuses = sorted(
        (await api.get(f"/appointments/{i}", headers=headers)).json()["status"] for i in ids
    )
    assert statuses == ["confirmed", "failed"]
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.BOOKED
```

The spec placed the round trip in T4. T4 has no fixtures yet and building that harness is not this slice's scope, so it runs in T3 with an in-process worker; say so in the commit body.

- [ ] **Step 4: Run** with the stack up: `python -m pytest tests/tiers/t3_integration/test_booking_round_trip.py -q` → 4 passed. Then the full docker lane.

- [ ] **Step 5: Verify in containers.** `docker compose --profile app -f docker-compose.infra.yml up -d --build --wait`; seed with `docker compose --profile app -f docker-compose.infra.yml exec app python -m app.cli seed --password "Passw0rd-demo"`; create a front-desk user via `python -m app.cli create-user` in the same container; log in with `POST /auth/login`; pick a free slot and its patient with `docker compose ... exec postgres psql -U smarthealth -c "select id from provider_slots where status='free' limit 1"` and `"select id from patients limit 1"`; `POST /appointments`; poll `GET /appointments/{id}` until `confirmed`. Record the commands and outputs in the report. Bring the app profile down without `-v`.

- [ ] **Step 6: Commit** — `feat: run real booking activities in the worker, proven end to end`.

---

### Task 10: Full verification

```bash
python -m pytest -m "not docker" -q
python -m pytest -m docker -q
python -m ruff check app tests
python -m tests.runner.route_check
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth python -m alembic check
```

All green; record counts for Task 12.

---

### Task 11: Cases for the Stop hook

Author with the `smarthealth-testcase` skill; verify each requirement id exists in `docs/PRD.md`:

| Id | Behaviour | Tier | Priority | Requirement | impl |
| --- | --- | --- | --- | --- | --- |
| `bkg-001-confirmed-only-after-every-step` | the workflow confirms only after all steps, in order | workflow | P0 | `PART-A-FR-2` | `tests/tiers/t2_workflow/test_booking_workflow.py::test_confirms_only_after_every_step_in_order` |
| `bkg-002-a-failed-step-compensates` | any failing step runs compensation and never confirms | workflow | P0 | `PART-A-NFR-2` | `...::test_any_failing_step_compensates_and_never_confirms` |
| `bkg-003-one-slot-one-winner` | two concurrent claims on one slot: exactly one wins | integration | P0 | `PART-A-NFR-1` | `tests/tiers/t3_integration/test_booking_service.py::test_two_concurrent_claims_have_exactly_one_winner` |
| `bkg-004-a-repeat-books-once` | a repeated request with one Idempotency-Key books once | integration | P0 | `PART-A-FR-2` | `tests/tiers/t3_integration/test_booking_round_trip.py::test_a_repeated_request_books_once` |
| `bkg-005-booking-end-to-end` | HTTP → worker → confirmed, slot booked, audit trail written | integration | P1 | `PART-A-FR-2` | `...test_booking_round_trip.py::test_a_booking_is_confirmed_end_to_end` |

`route_check` → 22 cases, none unsupported or blocked. Commit `test: add cases for the booking workflow`. If the `workflow` tier needs routing support the runner lacks, report it rather than retiering the case.

---

### Task 12: Documentation — only what now exists

Verify every claim against code (grep before asserting).

- `docs/PRD.md`: FR-2 status → "**In progress** — booking built (slice 2); cancel/reschedule slice 4"; R-3 and R-4 → **Closed**, naming `service.ensure_slot_bookable` / `service.claim_slot` and the tests that prove them; R-2 → restate per spec §2 and mark "booking/compensation paths done; cancel/reschedule/visit paths pending"; add D-9, D-10, D-11, D-15 to §8 (copy rationale from the spec); §11 module map FR-2 → list the new scheduling files; recount the §11 coverage table.
- `docs/ARCHITECTURE.md`: §3.2 banner → built, with the actual file:line of each step; the diagram's "Publish appointments.booked" step stays marked as Week 3. §1.4 scheduling module entry: the new files. §1.3: the declared edges onto `patients.public` and why service.py takes none. §3 top banner: business workflow now exists (booking only). Add a §2 data flow for `POST /appointments` in the style of §2.3.
- `README.md`: endpoint/role table gains `POST /appointments` (patient-own, front_desk, admin → 202) and `GET /appointments/{id}`; "Running it" gains the booking demo from Task 9 Step 5.
- `CLAUDE.md`: status paragraph (booking landed); remove "Week 2's `scheduling/service.py` is the first code with a real reason to break this" and state the actual resolution (activities and router use `patients.public`; service takes none); test count.

Commit `docs: record the booking workflow and close R-3 and R-4`.

---

### Task 13: Land

Re-run Task 10. Try `gh pr create`; on failure fast-forward `main` and push — **only with the user's go-ahead in the session**.

---

## Inherited by slice 3 (visit workflow)

- Booking's step 7 — start `visit-<appointment_id>` — is not in this slice: `VisitWorkflow` does not exist yet. Slice 3 adds it as the last step after `confirm`, plus the visit activities. The `confirm`-is-last property must survive: starting the visit workflow happens **after** confirmation and its failure must not un-confirm.
- `state.py` gains the visit machine; `service.py` gains the completion/no-show paired writes (slot stays `booked`, per spec §2's R-2 table).
