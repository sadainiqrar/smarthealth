"""`python -m app.cli create-user` against a real database.

This is the only way to create an account outside of a test fixture, which is what
makes the running system demonstrable to a human reviewer rather than provable only
to the test suite.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.cli import create_user, link_patient
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password, verify_password
from app.modules.patients.models import Patient

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_create_user_inserts_a_usable_account(db_settings, db_session):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"
    await create_user(
        settings=db_settings, email=email, password="a strong secret", role="admin"
    )

    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.role is UserRole.ADMIN
    assert user.is_active is True
    assert verify_password("a strong secret", user.password_hash)


async def test_create_user_refuses_a_duplicate_email(db_settings):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"
    await create_user(settings=db_settings, email=email, password="x1", role="admin")

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x2", role="admin")
    assert exit_info.value.code == 1


async def test_create_user_refuses_an_invalid_role(db_settings):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x1", role="wizard")
    assert exit_info.value.code == 1


async def test_create_user_writes_no_row_for_an_address_that_cannot_log_in(
    db_settings, db_session
):
    """The regression this guards: the CLI used to accept `.test` and write a row
    whose owner could never authenticate, because `LoginRequest.email` is an
    `EmailStr` and `email-validator` refuses reserved TLDs. T0 proves the check
    fires before any database work; this proves the table really stays empty.
    """
    email = f"cli-{uuid.uuid4().hex[:8]}@medinova.test"

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x1", role="admin")
    assert exit_info.value.code == 1

    assert await db_session.scalar(select(User).where(User.email == email)) is None


# --- link-patient ---------------------------------------------------------------------


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def linking(factory):
    """Rows `link-patient` works on, committed and removed afterwards (the database is
    shared, and the seed tests count whole tables)."""
    made: dict[str, list] = {"users": [], "patients": []}

    async def user(role: UserRole = UserRole.PATIENT) -> User:
        async with factory() as session:
            row = User(
                email=f"link-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("pw-not-used"), role=role,
            )
            session.add(row)
            await session.commit()
        made["users"].append(row.id)
        return row

    async def patient(user_id: uuid.UUID | None = None) -> Patient:
        async with factory() as session:
            row = Patient(
                mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe",
                user_id=user_id,
            )
            session.add(row)
            await session.commit()
        made["patients"].append(row.id)
        return row

    yield SimpleNamespace(user=user, patient=patient)
    async with factory() as session:
        await session.execute(delete(Patient).where(Patient.id.in_(made["patients"])))
        await session.execute(delete(User).where(User.id.in_(made["users"])))
        await session.commit()


async def _linked_user(factory, patient_id: uuid.UUID) -> uuid.UUID | None:
    async with factory() as session:
        return await session.scalar(select(Patient.user_id).where(Patient.id == patient_id))


async def _refused(db_settings, capsys, email: str, mrn: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        await link_patient(settings=db_settings, email=email, mrn=mrn)
    assert exit_info.value.code == 1
    return capsys.readouterr().err


async def test_link_patient_links_the_login_audits_it_and_me_then_works(
    db_settings, factory, linking, audit_documents, api, token_for, capsys
):
    user, patient = await linking.user(), await linking.patient()

    await link_patient(settings=db_settings, email=user.email, mrn=patient.mrn)

    assert await _linked_user(factory, patient.id) == user.id
    assert str(user.id) in capsys.readouterr().out
    entry = await audit_documents.find_one({"entity_id": str(patient.id)})
    assert entry["action"] == "account_linked"
    assert entry["actor_user_id"] == "system:cli"
    assert entry["before"]["user_id"] is None and entry["after"]["user_id"] == str(user.id)
    me = await api.get(
        "/patients/me", headers=token_for(UserRole.PATIENT, subject=str(user.id))
    )
    assert me.status_code == 200 and me.json()["id"] == str(patient.id)


async def test_linking_the_same_pair_again_is_a_no_op_with_one_audit_entry(
    db_settings, factory, linking, audit_documents, capsys
):
    user, patient = await linking.user(), await linking.patient()

    await link_patient(settings=db_settings, email=user.email, mrn=patient.mrn)
    first = capsys.readouterr().out
    await link_patient(settings=db_settings, email=user.email, mrn=patient.mrn)
    second = capsys.readouterr().out

    assert first.startswith("linked ")
    assert second.startswith("already linked: ")
    assert await _linked_user(factory, patient.id) == user.id
    assert await audit_documents.count_documents({"entity_id": str(patient.id)}) == 1


async def test_an_unknown_email_is_refused(db_settings, linking, capsys):
    patient = await linking.patient()
    err = await _refused(db_settings, capsys, "nobody-here@example.com", patient.mrn)
    assert "nobody-here@example.com" in err


async def test_an_unknown_mrn_is_refused_and_links_nothing(
    db_settings, linking, audit_documents, capsys
):
    user = await linking.user()
    err = await _refused(db_settings, capsys, user.email, "MRN-DOES-NOT-EXIST")
    assert "MRN-DOES-NOT-EXIST" in err
    assert await audit_documents.count_documents({}) == 0


@pytest.mark.parametrize("role", [UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN])
async def test_a_login_that_is_not_a_patient_is_refused(
    db_settings, factory, linking, capsys, role
):
    user, patient = await linking.user(role), await linking.patient()
    err = await _refused(db_settings, capsys, user.email, patient.mrn)
    assert role.value in err
    assert await _linked_user(factory, patient.id) is None


async def test_a_record_already_linked_to_a_different_user_is_refused_and_kept(
    db_settings, factory, linking, audit_documents, capsys
):
    owner, intruder = await linking.user(), await linking.user()
    patient = await linking.patient(user_id=owner.id)

    err = await _refused(db_settings, capsys, intruder.email, patient.mrn)

    assert "already linked to a different user" in err
    assert await _linked_user(factory, patient.id) == owner.id
    assert await audit_documents.count_documents({}) == 0


async def test_a_user_already_linked_to_another_patient_is_refused(
    db_settings, factory, linking, audit_documents, capsys
):
    user = await linking.user()
    first = await linking.patient(user_id=user.id)
    second = await linking.patient()

    err = await _refused(db_settings, capsys, user.email, second.mrn)

    assert first.mrn in err
    assert await _linked_user(factory, second.id) is None
    assert await audit_documents.count_documents({}) == 0
