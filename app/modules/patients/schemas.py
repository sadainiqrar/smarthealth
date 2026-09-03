"""Patient request and response bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class PatientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mrn: str = Field(min_length=1, max_length=32)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None


class PatientUpdate(BaseModel):
    """A partial update. Every field is optional, but the body must not be empty."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _reject_an_explicit_null(cls, value: str | None) -> str | None:
        """`patients.first_name` and `last_name` are NOT NULL.

        Without this an explicit `{"first_name": null}` validates, `exclude_unset`
        reports it as set, and the service writes NULL into a NOT NULL column -- a
        client-triggerable 500 instead of a 422. `min_length` does not cover it: it
        constrains only the `str` branch of the union. Unset fields never reach a
        field validator, so an ordinary partial update is unaffected.
        """
        if value is None:
            raise ValueError("may not be set to null")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> PatientUpdate:
        if not self.model_dump(exclude_unset=True):
            raise ValueError("a patient update must set at least one field")
        return self


class PatientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mrn: str
    first_name: str
    last_name: str
    date_of_birth: date | None
    phone: str | None
    email: str | None
    user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
