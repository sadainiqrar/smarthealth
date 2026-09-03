import pytest
from pydantic import ValidationError

from app.modules.patients.schemas import PatientCreate, PatientUpdate

pytestmark = pytest.mark.unit


def test_create_requires_identifying_fields():
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Jo")


def test_create_accepts_the_minimum():
    patient = PatientCreate(mrn="MRN-1", first_name="Jo", last_name="Bloggs")
    assert patient.date_of_birth is None
    assert patient.phone is None


def test_create_rejects_a_blank_mrn():
    with pytest.raises(ValidationError):
        PatientCreate(mrn="   ", first_name="Jo", last_name="Bloggs")


def test_create_rejects_a_malformed_email():
    with pytest.raises(ValidationError):
        PatientCreate(
            mrn="MRN-1", first_name="Jo", last_name="Bloggs", email="nope"
        )


def test_update_reports_only_the_fields_actually_sent():
    """A PATCH that set every unspecified field to None would erase data."""
    update = PatientUpdate(phone="+44 20 7946 0000")
    assert update.model_dump(exclude_unset=True) == {"phone": "+44 20 7946 0000"}


def test_update_rejects_an_empty_body():
    with pytest.raises(ValidationError, match="at least one field"):
        PatientUpdate()
