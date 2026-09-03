"""Patient request and response bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


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
