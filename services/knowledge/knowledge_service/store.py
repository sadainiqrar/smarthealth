"""The knowledge service's own copy of the provider directory.

Built from the compacted `providers.state` topic (event-carried state transfer, spec
section 1.1): the service reads from offset 0 and has every provider, and never queries Part
A's tables. In Week 4 the same rows are embedded into Qdrant; today `search` is a plain
text match, the stand-in for retrieval behind an unchanging contract.

Two implementations, one interface: Postgres (the service's own database) and in memory
for the contract tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import JSON, Boolean, Column, MetaData, String, Table, delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

directory = Table(
    "provider_directory", metadata,
    Column("provider_id", String(36), primary_key=True),
    Column("name", String(250), nullable=False),
    Column("specialty", String(120), nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("department_ids", JSON, nullable=False),
)


@dataclass(frozen=True)
class Entry:
    provider_id: str
    name: str
    specialty: str
    is_active: bool


def entry_from_state(payload: dict) -> Entry:
    return Entry(
        provider_id=str(payload["provider_id"]),
        name=f"{payload['first_name']} {payload['last_name']}",
        specialty=payload["specialty"], is_active=bool(payload["is_active"]),
    )


class Directory(Protocol):
    async def upsert(self, entry: Entry, department_ids: list[str]) -> None: ...
    async def remove(self, provider_id: str) -> None: ...
    async def search(self, query: str, limit: int) -> list[Entry]: ...


class PostgresDirectory:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self) -> None:
        """The skeleton's schema; Week 4 moves it to the service's own migrations."""
        async with self._engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def upsert(self, entry: Entry, department_ids: list[str]) -> None:
        values = {"provider_id": entry.provider_id, "name": entry.name,
                  "specialty": entry.specialty, "is_active": entry.is_active,
                  "department_ids": department_ids}
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(directory).values(**values).on_conflict_do_update(
                    index_elements=[directory.c.provider_id], set_=values)
            )

    async def remove(self, provider_id: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(directory).where(directory.c.provider_id == provider_id)
            )

    async def search(self, query: str, limit: int) -> list[Entry]:
        pattern = f"%{query.strip()}%"
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(directory).where(
                    directory.c.is_active.is_(True),
                    or_(directory.c.specialty.ilike(pattern), directory.c.name.ilike(pattern)),
                ).order_by(directory.c.specialty, directory.c.name).limit(limit)
            )
            return [Entry(r.provider_id, r.name, r.specialty, r.is_active) for r in rows]


class InMemoryDirectory:
    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}

    async def upsert(self, entry: Entry, department_ids: list[str]) -> None:
        self.entries[entry.provider_id] = entry

    async def remove(self, provider_id: str) -> None:
        self.entries.pop(provider_id, None)

    async def search(self, query: str, limit: int) -> list[Entry]:
        needle = query.strip().lower()
        found = [e for e in self.entries.values()
                 if e.is_active and (needle in e.specialty.lower() or needle in e.name.lower())]
        return sorted(found, key=lambda e: (e.specialty, e.name))[:limit]
