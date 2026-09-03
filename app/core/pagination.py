"""Offset pagination types for list endpoints.

This module has zero framework imports on purpose, for the same reason as
`app.core.errors` and `app.core.audit`: importing it must not pull FastAPI, Starlette,
or the ASGI stack into a process that has no business loading them (a Temporal worker, a
Celery task, a plain script). `PageParams` and `Page` are plain dataclass/pydantic
types a service can build and return without ever knowing a request happened.

The FastAPI dependency that parses `limit`/`offset` off the query string into a
`PageParams` lives in `app.api.deps`, not here — see `page_params` there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

#: A single request must not be able to ask the database for every row.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


class Page(BaseModel, Generic[T]):
    """One slice of a result set, plus enough context to fetch the next."""

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
