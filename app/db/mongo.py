"""MongoDB client and the audit collection.

Mongo owns the audit trail: append-only, high volume, and a before/after payload
whose shape differs per entity and per action. Postgres remains the system of record
for every piece of transactional state (design spec 3.4).

Uses PyMongo's async driver rather than Motor: Motor was deprecated in May 2026 in
favour of this, and the switch was made while the footprint was one file.
"""

from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection

from app.settings import Settings

AUDIT_COLLECTION = "audit_events"


def create_mongo_client(settings: Settings) -> AsyncMongoClient:
    """Connects lazily, so this is safe to call with no Mongo running."""
    return AsyncMongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000,
        uuidRepresentation="standard",
    )


def get_audit_collection(client: AsyncMongoClient, settings: Settings) -> AsyncCollection:
    return client[settings.mongo_db][AUDIT_COLLECTION]


async def ensure_audit_indexes(client: AsyncMongoClient, settings: Settings) -> None:
    """Create the indexes the audit query pattern needs.

    Idempotent — Mongo ignores a create for an index that already exists. Called
    explicitly by tooling, not at startup, so application boot needs no database.
    """
    collection = get_audit_collection(client, settings)
    await collection.create_index([("entity_type", 1), ("entity_id", 1), ("at", -1)])
    await collection.create_index([("at", -1)])
