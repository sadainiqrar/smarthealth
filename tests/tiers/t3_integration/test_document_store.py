from datetime import UTC, datetime

import pytest

from app.db.mongo import create_mongo_client, ensure_audit_indexes, get_audit_collection
from app.db.redis import create_redis_client, namespaced_key
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_an_audit_event_round_trips(db_settings: Settings):
    """Mongo owns the audit trail; the payload shape varies per entity by design."""
    client = create_mongo_client(db_settings)
    try:
        collection = get_audit_collection(client, db_settings)
        document = {
            "entity_type": "patient",
            "entity_id": "11111111-1111-1111-1111-111111111111",
            "action": "profile_updated",
            "at": datetime.now(UTC),
            "before": {"phone": None},
            "after": {"phone": "+44 20 7946 0000"},
        }
        await collection.insert_one(document)

        stored = await collection.find_one({"entity_type": "patient"})
        assert stored["action"] == "profile_updated"
        assert stored["after"]["phone"] == "+44 20 7946 0000"
        assert stored["before"]["phone"] is None
    finally:
        await get_audit_collection(client, db_settings).drop()
        await client.close()


async def test_audit_documents_of_different_shapes_coexist(db_settings: Settings):
    """The reason this lives in a document store: no shared column set."""
    client = create_mongo_client(db_settings)
    try:
        collection = get_audit_collection(client, db_settings)
        await collection.insert_many(
            [
                {
                    "entity_type": "appointment",
                    "entity_id": "a1",
                    "action": "confirmed",
                    "at": datetime.now(UTC),
                    "after": {"status": "confirmed", "slot_id": "s1"},
                },
                {
                    "entity_type": "provider",
                    "entity_id": "p1",
                    "action": "schedule_changed",
                    "at": datetime.now(UTC),
                    "before": {"departments": ["cardiology"]},
                    "after": {"departments": ["cardiology", "neurology"]},
                },
            ]
        )
        assert await collection.count_documents({}) == 2
        provider_event = await collection.find_one({"entity_type": "provider"})
        assert provider_event["after"]["departments"] == ["cardiology", "neurology"]
    finally:
        await get_audit_collection(client, db_settings).drop()
        await client.close()


async def test_the_audit_indexes_exist(db_settings: Settings):
    client = create_mongo_client(db_settings)
    try:
        await ensure_audit_indexes(client, db_settings)
        collection = get_audit_collection(client, db_settings)
        cursor = await collection.list_indexes()
        names = [index["name"] for index in await cursor.to_list()]
        assert any("entity_type" in name for name in names)
    finally:
        await get_audit_collection(client, db_settings).drop()
        await client.close()


async def test_redis_round_trips_a_namespaced_key(db_settings: Settings):
    """The prefix is the only thing keeping two concurrent runs apart in Redis."""
    client = create_redis_client(db_settings)
    try:
        key = namespaced_key(db_settings, "probe:slot-hold")
        assert key.startswith(db_settings.redis_prefix)
        await client.set(key, "held", ex=30)
        assert await client.get(key) == "held"
        await client.delete(key)
        assert await client.get(key) is None
    finally:
        await client.aclose()


async def test_redis_responds_to_ping(db_settings: Settings):
    client = create_redis_client(db_settings)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
