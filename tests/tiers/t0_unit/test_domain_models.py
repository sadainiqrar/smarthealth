import pytest

from app.modules.patients.models import Patient
from app.modules.providers.models import (
    Clinic,
    Department,
    Provider,
    ProviderSlot,
    provider_departments,
)

pytestmark = pytest.mark.unit


def test_a_patient_can_exist_without_a_user_account():
    """Front-desk staff register walk-ins who have no credentials (spec 3.1)."""
    assert Patient.__table__.c.user_id.nullable
    assert Patient.__table__.c.user_id.unique


def test_patient_medical_record_number_is_unique():
    assert Patient.__table__.c.mrn.unique


def test_a_provider_can_exist_without_a_user_account():
    assert Provider.__table__.c.user_id.nullable
    assert Provider.__table__.c.user_id.unique


def test_a_department_belongs_to_one_clinic():
    """Cardiology at Riverside is a different unit from Cardiology at Northgate."""
    assert not Department.__table__.c.clinic_id.nullable
    names = {c.name for c in Department.__table__.constraints}
    assert "uq_departments_clinic_id_name" in names


def test_providers_and_departments_are_many_to_many():
    """A provider covering two clinics must remain bookable at both."""
    assert set(provider_departments.c.keys()) == {"provider_id", "department_id"}
    assert len(provider_departments.primary_key.columns) == 2


def test_a_clinic_records_its_timezone():
    """Slot generation is wrong without it once clinics span zones."""
    assert not Clinic.__table__.c.timezone.nullable


def test_tables_are_named_as_expected():
    assert Patient.__tablename__ == "patients"
    assert Clinic.__tablename__ == "clinics"
    assert Department.__tablename__ == "departments"
    assert Provider.__tablename__ == "providers"


def _ddl(table) -> str:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_slot_status_is_constrained_at_the_database_level():
    """`native_enum=False` alone emits a bare VARCHAR: SQLAlchemy 2.0 defaults
    create_constraint to False, so without it the database accepts any string."""
    ddl = _ddl(ProviderSlot.__table__)
    assert "CHECK" in ddl
    for value in ("free", "held", "booked", "blocked"):
        assert f"'{value}'" in ddl


def test_user_role_is_constrained_at_the_database_level():
    from app.modules.identity.models import User

    ddl = _ddl(User.__table__)
    assert "CHECK" in ddl
    for value in ("patient", "provider", "front_desk", "admin"):
        assert f"'{value}'" in ddl
