"""Offset pagination for list endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

#: A single request must not be able to ask the database for every row.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> PageParams:
    """FastAPI dependency for the two query parameters."""
    return PageParams(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    """One slice of a result set, plus enough context to fetch the next."""

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
