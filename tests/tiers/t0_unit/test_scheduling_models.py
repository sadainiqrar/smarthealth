import pytest

from app.db.all_models import ALL_TABLES
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    Visit,
    VisitStatus,
    WaitlistEntry,
)

pytestmark = pytest.mark.unit


def _ddl(table) -> str:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_appointment_starts_pending_not_confirmed():
    """The headline requirement: confirmed only after the whole workflow succeeds."""
    assert Appointment.__table__.c.status.server_default.arg.text == "'pending'"


def test_appointment_statuses_cover_the_required_lifecycle():
    assert {status.value for status in AppointmentStatus} == {
        "pending", "confirmed", "cancelled", "rescheduled",
        "completed", "no_show", "failed",
    }


def test_a_reschedule_links_back_to_the_appointment_it_replaced():
    """History matters to both the audit trail and the analytics."""
    column = Appointment.__table__.c.rescheduled_from_id
    assert column.nullable
    assert {fk.column.table.name for fk in column.foreign_keys} == {"appointments"}


def test_a_visit_is_one_to_one_with_an_appointment():
    assert Visit.__table__.c.appointment_id.unique


def test_wait_time_components_are_present_and_optional_after_check_in():
    """Average Wait Time = seen_at - checked_in_at."""
    assert not Visit.__table__.c.checked_in_at.nullable
    assert Visit.__table__.c.seen_at.nullable
    assert Visit.__table__.c.completed_at.nullable


def test_visit_statuses():
    assert {status.value for status in VisitStatus} == {
        "checked_in", "in_progress", "completed"
    }


def test_waitlist_entry_targets_a_provider_or_a_department():
    for name in ("provider_id", "department_id"):
        assert WaitlistEntry.__table__.c[name].nullable


def test_every_scheduling_status_is_constrained_at_the_database_level():
    """Enum(native_enum=False) emits a bare VARCHAR unless create_constraint=True."""
    for table, values in (
        (Appointment.__table__, ("pending", "confirmed", "failed")),
        (Visit.__table__, ("checked_in", "in_progress", "completed")),
        (WaitlistEntry.__table__, ("active", "fulfilled", "expired", "cancelled")),
    ):
        ddl = _ddl(table)
        assert "CHECK" in ddl, f"{table.name} has no CHECK constraint"
        for value in values:
            assert f"'{value}'" in ddl, f"{table.name} CHECK omits {value}"


def test_all_ten_persistent_tables_are_registered_for_migrations():
    """Alembic only sees tables whose module has been imported.

    `<=` rather than `==`: test_db_base.py defines two probe tables on the same
    metadata, and whether they are present depends on import order.
    """
    expected = {
        "users", "patients", "providers", "clinics", "departments",
        "provider_departments", "provider_slots", "appointments", "visits",
        "waitlist_entries",
    }
    assert len(expected) == 10
    assert expected <= ALL_TABLES
