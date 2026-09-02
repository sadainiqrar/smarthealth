import pytest

from app.db.mongo import AUDIT_COLLECTION, create_mongo_client, get_audit_collection
from app.db.redis import create_redis_client, namespaced_key
from app.settings import Settings

pytestmark = pytest.mark.unit


async def test_mongo_client_is_built_without_connecting():
    """PyMongo's async client connects lazily; the T1 lane must not require a running Mongo."""
    settings = Settings(mongo_host="203.0.113.1", mongo_port=1, mongo_db="sh_test")
    client = create_mongo_client(settings)
    try:
        assert client is not None
    finally:
        await client.close()


async def test_audit_collection_is_selected_from_the_configured_database():
    settings = Settings(mongo_db="sh_run_db")
    client = create_mongo_client(settings)
    try:
        collection = get_audit_collection(client, settings)
        assert collection.name == AUDIT_COLLECTION
        assert collection.database.name == "sh_run_db"
    finally:
        await client.close()


def test_redis_client_is_built_without_connecting():
    settings = Settings(redis_host="203.0.113.1", redis_port=1, redis_db=4)
    client = create_redis_client(settings)
    assert client.connection_pool.connection_kwargs["db"] == 4


def test_namespaced_key_applies_the_isolation_prefix():
    """Two test runs against one Redis must not read each other's keys."""
    settings = Settings(redis_prefix="t_ab12_gw0_")
    assert namespaced_key(settings, "slot:hold:1") == "t_ab12_gw0_slot:hold:1"


def test_namespaced_key_is_a_no_op_without_a_prefix():
    assert namespaced_key(Settings(redis_prefix=""), "k") == "k"
