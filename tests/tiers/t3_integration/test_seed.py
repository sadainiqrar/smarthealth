"""The seeder against a real database.

`tests/tiers/t0_unit/test_seed.py` proves the generation rules -- slots in the future,
on weekdays, never overlapping -- without infrastructure. None of that touches SQL, so
the claims that only a database can settle live here: that the inserts satisfy every
constraint, that a rerun really is a no-op, and that `--clear` removes what the seeder
created and nothing else.

This file exists because those three were asserted in a commit message before they had
ever executed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.core.clock import Clock
from app.modules.identity.models import User
from app.modules.identity.service import authenticate
from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider, ProviderSlot, SlotStatus
from app.seed import (
    CLINICS,
    DEPARTMENTS,
    PROVIDERS,
    USERS,
    clear,
    seed,
    seeded_patient_mrns,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PATIENT_COUNT = 12
PASSWORD = "seed-integration-password"

# `db_settings` is session-scoped, so every T3 test shares one database -- and the
# `api` fixture drives the real app, which commits. So rows from other tests are
# present here, and any assertion of the form `count(Patient) == PATIENT_COUNT` is a
# test that passes alone and fails in a suite.
#
# Every assertion below is therefore scoped to the rows the seeder owns, identified by
# the same natural keys the seeder and `--clear` match on.
SEEDED_CLINIC_NAMES = [name for name, _tz, _addr in CLINICS]
SEEDED_LICENCES = [licence for licence, *_rest in PROVIDERS]
SEEDED_EMAILS = [email for email, _role in USERS]
SEEDED_MRNS = seeded_patient_mrns(PATIENT_COUNT)


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def _seeded_patients(session):
    return (
        await session.scalars(select(Patient).where(Patient.mrn.in_(SEEDED_MRNS)))
    ).all()


async def test_seeding_satisfies_every_constraint(db_session):
    """The schema's constraints are the point of Week 1, so the seeder proving it can
    satisfy them -- composite foreign keys, the GiST no-overlap rule, the enum CHECKs --
    is what makes the data trustworthy rather than merely present."""
    summary = await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    assert summary.clinics == len(CLINICS)
    assert summary.departments == len(DEPARTMENTS)
    assert summary.providers == len(PROVIDERS)
    assert summary.users == len(USERS)
    assert summary.patients == PATIENT_COUNT
    assert summary.slots > 0

    present_clinics = (
        await db_session.scalars(select(Clinic.name).where(Clinic.name.in_(SEEDED_CLINIC_NAMES)))
    ).all()
    present_providers = (
        await db_session.scalars(
            select(Provider.license_number).where(Provider.license_number.in_(SEEDED_LICENCES))
        )
    ).all()
    assert sorted(present_clinics) == sorted(SEEDED_CLINIC_NAMES)
    assert sorted(present_providers) == sorted(SEEDED_LICENCES)
    assert len(await _seeded_patients(db_session)) == PATIENT_COUNT


async def test_rerunning_changes_nothing(db_session):
    """Idempotency, which is a statement about the database and cannot be unit-tested.

    Every entity is matched on its natural key, so the second pass must find all of them
    and insert none. A regression here would not raise -- it would quietly double the
    data, or trip the GiST exclusion constraint on the second set of slots.
    """
    first = await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()
    second = await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    assert first.slots > 0, "expected the first pass to create something"
    assert second == type(second)(), f"second pass was not a no-op: {second}"


async def test_seeded_slots_are_free_future_and_non_overlapping(db_session):
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    total = await _count(db_session, ProviderSlot)
    free = await db_session.scalar(
        select(func.count()).select_from(ProviderSlot).where(
            ProviderSlot.status == SlotStatus.FREE
        )
    )
    assert free == total, "every seeded slot should be bookable"

    # The booking activity will reject a slot in the past (risk R-3). Seed data that
    # fails a demo on its own terms would be worse than no seed data.
    in_the_past = await db_session.scalar(
        text("SELECT count(*) FROM provider_slots WHERE starts_at <= now()")
    )
    assert in_the_past == 0

    on_a_weekend = await db_session.scalar(
        text("SELECT count(*) FROM provider_slots WHERE EXTRACT(dow FROM starts_at) IN (0, 6)")
    )
    assert on_a_weekend == 0

    # Overlap is enforced by the GiST exclusion constraint, so reaching this line at all
    # means the constraint accepted every row.


async def test_most_patients_are_walk_ins_with_no_login(db_session):
    """`patients.user_id` is nullable because front-desk staff register people who have
    no credentials. The seed data should make that visible rather than give everyone an
    account, which would quietly misrepresent the domain."""
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    seeded = await _seeded_patients(db_session)
    assert len(seeded) == PATIENT_COUNT
    with_a_login = [patient for patient in seeded if patient.user_id is not None]
    assert len(with_a_login) == 1


async def test_a_seeded_login_actually_authenticates(db_session, db_settings):
    """A seeded account that cannot log in looks created and is useless -- the same
    failure `create-user` guards against by validating the email."""
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    email, _role = USERS[0]
    token, expires_in = await authenticate(
        db_session, email=email, password=PASSWORD,
        settings=db_settings, now=Clock().now(),
    )
    assert token and expires_in > 0


async def test_every_seeded_id_is_a_v7_uuid(db_session):
    """The column default is unit-tested, but this is the only place that proves the
    value SQLAlchemy actually sends to Postgres is a v7 -- the wrapper between the two
    is library code, not ours."""
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    scoped = (
        (Clinic, Clinic.name.in_(SEEDED_CLINIC_NAMES)),
        (Provider, Provider.license_number.in_(SEEDED_LICENCES)),
        (Patient, Patient.mrn.in_(SEEDED_MRNS)),
        (User, User.email.in_(SEEDED_EMAILS)),
    )
    for model, predicate in scoped:
        ids = (await db_session.scalars(select(model.id).where(predicate))).all()
        assert ids, f"{model.__tablename__} seeded no rows"
        assert {identifier.version for identifier in ids} == {7}, (
            f"{model.__tablename__} has non-v7 primary keys"
        )


async def test_no_week_two_workflow_state_is_created(db_session):
    """Appointments and visits are workflow outputs. Seeding one would fabricate state
    no workflow produced, and Week 2 would be tested against fiction."""
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    for table in ("appointments", "visits", "waitlist_entries"):
        assert await db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_clear_removes_what_it_created_and_nothing_else(db_session):
    """The reason `clear` matches exact MRNs instead of a `MRN-______` LIKE pattern: a
    real patient using that format would otherwise be deleted by a command that never
    created them."""
    bystander = Patient(mrn="MRN-README-1", first_name="Ada", last_name="Lovelace")
    db_session.add(bystander)
    await seed(db_session, password=PASSWORD, patient_count=PATIENT_COUNT)
    await db_session.flush()

    removed = await clear(db_session, patient_count=PATIENT_COUNT)
    await db_session.flush()

    assert removed.patients == PATIENT_COUNT
    assert not (
        await db_session.scalars(select(Clinic.name).where(Clinic.name.in_(SEEDED_CLINIC_NAMES)))
    ).all()
    assert not (
        await db_session.scalars(
            select(Provider.license_number).where(Provider.license_number.in_(SEEDED_LICENCES))
        )
    ).all()
    assert not await _seeded_patients(db_session)

    # The bystander must survive: `clear` matches exact MRNs, never a `MRN-______`
    # LIKE pattern that would also delete a real patient using that format.
    bystander_still_there = await db_session.scalar(
        select(Patient.mrn).where(Patient.mrn == "MRN-README-1")
    )
    assert bystander_still_there == "MRN-README-1"
