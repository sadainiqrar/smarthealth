"""Validation rules for the provider request bodies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.providers.schemas import ProviderCreate, ProviderUpdate

pytestmark = pytest.mark.unit


def test_create_requires_identifying_fields():
    with pytest.raises(ValidationError):
        ProviderCreate(first_name="Ada")


def test_create_accepts_the_minimum():
    provider = ProviderCreate(
        first_name="Ada",
        last_name="Lovelace",
        specialty="Cardiology",
        license_number="LIC-1",
    )
    assert provider.is_active is True


def test_create_rejects_a_blank_license_number():
    """`str_strip_whitespace` reduces "   " to "", which `min_length=1` then rejects."""
    with pytest.raises(ValidationError):
        ProviderCreate(
            first_name="Ada",
            last_name="Lovelace",
            specialty="Cardiology",
            license_number="   ",
        )


def test_create_rejects_a_blank_specialty():
    with pytest.raises(ValidationError):
        ProviderCreate(
            first_name="Ada",
            last_name="Lovelace",
            specialty="  ",
            license_number="LIC-1",
        )


def test_update_reports_only_the_fields_actually_sent():
    """A PATCH that set every unspecified field to None would erase data."""
    update = ProviderUpdate(specialty="Neurology")
    assert update.model_dump(exclude_unset=True) == {"specialty": "Neurology"}


def test_update_rejects_an_empty_body():
    with pytest.raises(ValidationError, match="at least one field"):
        ProviderUpdate()


def test_update_allows_deactivation():
    update = ProviderUpdate(is_active=False)
    assert update.is_active is False
    assert update.model_dump(exclude_unset=True) == {"is_active": False}


def test_update_allows_reactivation():
    """The mirror of deactivation: `True` is an ordinary value, not a no-op."""
    update = ProviderUpdate(is_active=True)
    assert update.model_dump(exclude_unset=True) == {"is_active": True}


def test_update_cannot_change_the_licence_number():
    """A licence number identifies the clinician to a regulator; changing it silently
    would break the audit trail's link to the real person."""
    assert "license_number" not in ProviderUpdate.model_fields


@pytest.mark.parametrize("field", sorted(ProviderUpdate.model_fields))
def test_update_rejects_an_explicit_null_for_every_field(field: str):
    """Every column `ProviderUpdate` can touch is NOT NULL, so an explicit null must
    fail validation (422) rather than the flush (500). Parametrised over the model's
    own fields so a field added later without the validator fails here."""
    with pytest.raises(ValidationError):
        ProviderUpdate(**{field: None})


def test_update_rejects_a_null_is_active_but_not_a_false_one():
    """The trap this pins: a validator written as `if not value` would reject
    `is_active=False` -- a legitimate deactivation -- along with the null."""
    assert ProviderUpdate(is_active=False).is_active is False
    with pytest.raises(ValidationError):
        ProviderUpdate(is_active=None)


def test_update_still_accepts_an_ordinary_partial_update():
    """The validator must not fire on unset fields or on real values."""
    update = ProviderUpdate(first_name="Ada")
    assert update.model_dump(exclude_unset=True) == {"first_name": "Ada"}


def test_update_accepts_several_fields_at_once():
    update = ProviderUpdate(first_name="Ada", last_name="Lovelace", is_active=False)
    assert update.model_dump(exclude_unset=True) == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "is_active": False,
    }


def test_a_licence_number_sent_to_update_is_ignored_not_written():
    """Pydantic's default `extra="ignore"` drops it before the model exists, so it can
    never reach the service's setattr loop."""
    update = ProviderUpdate(specialty="Neurology", license_number="SHOULD-BE-IGNORED")
    assert update.model_dump(exclude_unset=True) == {"specialty": "Neurology"}
    assert not hasattr(update, "license_number")


def test_update_rejects_a_blank_string_field():
    """`min_length=1` still applies to the `str` branch of each union."""
    with pytest.raises(ValidationError):
        ProviderUpdate(first_name="   ")
