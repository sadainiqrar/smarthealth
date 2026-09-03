"""Provider request and response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    specialty: str = Field(min_length=1, max_length=120)
    license_number: str = Field(min_length=1, max_length=64)
    is_active: bool = True


class ProviderUpdate(BaseModel):
    """A partial update.

    `license_number` is deliberately absent: it identifies the clinician to a
    regulator, and silently changing it would break the audit trail's link to a real
    person. Correcting one is a deliberate, separate operation.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    specialty: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None

    @field_validator("first_name", "last_name", "specialty", "is_active")
    @classmethod
    def _reject_an_explicit_null(cls, value: object) -> object:
        """Every column this schema can touch is NOT NULL.

        Without this an explicit `{"specialty": null}` validates, `exclude_unset`
        reports it as set, and the service writes NULL into a NOT NULL column -- a
        client-triggerable 500 instead of a 422. `min_length` does not cover it: it
        constrains only the `str` branch of the union. Unset fields never reach a
        field validator, so a normal partial update is unaffected.

        The `is None` test is load-bearing for `is_active`: written as `if not value`
        it would also reject `is_active=False`, which is how a provider is
        deactivated -- the single most likely edit to this model.
        """
        if value is None:
            raise ValueError("may not be set to null")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ProviderUpdate:
        if not self.model_dump(exclude_unset=True):
            raise ValueError("a provider update must set at least one field")
        return self


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    specialty: str
    license_number: str
    is_active: bool
    user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
