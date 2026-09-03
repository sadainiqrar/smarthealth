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


def test_update_rejects_an_explicit_null_first_name():
    """`patients.first_name` is NOT NULL; a null must fail validation, not the flush."""
    with pytest.raises(ValidationError):
        PatientUpdate(first_name=None)


def test_update_rejects_an_explicit_null_last_name():
    """`patients.last_name` is NOT NULL; a null must fail validation, not the flush."""
    with pytest.raises(ValidationError):
        PatientUpdate(last_name=None)


def test_update_still_accepts_an_explicit_null_phone():
    """`phone` is nullable -- clearing it is a legitimate operation."""
    update = PatientUpdate(phone=None)
    assert update.model_dump(exclude_unset=True) == {"phone": None}


def test_update_still_accepts_an_explicit_null_date_of_birth():
    """`date_of_birth` is nullable -- clearing it is a legitimate operation."""
    update = PatientUpdate(date_of_birth=None)
    assert update.model_dump(exclude_unset=True) == {"date_of_birth": None}


def test_update_still_accepts_an_explicit_null_email():
    """`email` is nullable -- clearing it is a legitimate operation."""
    update = PatientUpdate(email=None)
    assert update.model_dump(exclude_unset=True) == {"email": None}


def test_update_still_accepts_an_ordinary_partial_update():
    """The validator must not fire on unset fields or on real values."""
    update = PatientUpdate(first_name="Ada")
    assert update.model_dump(exclude_unset=True) == {"first_name": "Ada"}


def test_an_unknown_field_is_ignored_not_written():
    """`mrn` is deliberately absent from PatientUpdate; a client that sends it must not
    have it silently applied. Pydantic's default `extra="ignore"` drops it before the
    model exists, so it can never reach the service's setattr loop."""
    update = PatientUpdate(phone="+1 555 0100", mrn="SHOULD-BE-IGNORED")
    assert update.model_dump(exclude_unset=True) == {"phone": "+1 555 0100"}
    assert not hasattr(update, "mrn")
