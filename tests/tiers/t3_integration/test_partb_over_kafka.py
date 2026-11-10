"""Part A -> Kafka -> Part B, with no shared code or store in between.

Part A registers a provider; its outbox and relay publish `providers.state`; the knowledge
service's own consumer -- which imports nothing of Part A -- decodes it under the
registered Avro schema and has the provider in its own directory. And a deactivated login
reaches the assistant's deny-list the same way.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import tests.harness.partb  # noqa: F401
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events import kafka
from app.events.relay import Relay
from app.modules.identity.models import UserRole

from assistant_service.main import consume_revocations  # noqa: E402
from assistant_service.settings import AssistantSettings  # noqa: E402
from assistant_service.stores import InMemoryStores  # noqa: E402
from knowledge_service.consumer import run as run_directory  # noqa: E402
from knowledge_service.settings import KnowledgeSettings  # noqa: E402
from knowledge_service.store import InMemoryDirectory  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    kafka.ensure_topics(db_settings)
    kafka.register_schemas(db_settings)
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def _drain(settings, factory):
    relay = Relay(settings, factory)
    while (await relay.run_once()).published:
        pass


async def _until(predicate, timeout=45):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "never arrived"
        await asyncio.sleep(0.5)


async def test_knowledge_builds_its_directory_from_part_as_events(api, token_for, db_settings,
                                                                 factory):
    """Referenced by tests/cases/pb-004-part-b-learns-from-part-a-over-kafka-only.yaml."""
    licence = f"LIC-{uuid.uuid4().hex[:10]}"
    created = await api.post("/providers", headers=token_for(UserRole.ADMIN), json={
        "first_name": "Grace", "last_name": "Hopper", "specialty": f"nephrology-{licence}",
        "license_number": licence})
    assert created.status_code == 201, created.text
    await _drain(db_settings, factory)

    directory = InMemoryDirectory()
    settings = KnowledgeSettings(
        kafka_bootstrap_servers=db_settings.kafka_bootstrap_servers,
        schema_registry_url=db_settings.schema_registry_url,
        topic_prefix=db_settings.resource_prefix, kafka_group=f"test-{uuid.uuid4().hex[:8]}",
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_directory(settings, directory, stop))
    try:
        async def present():
            return bool(await directory.search(licence, 5))

        await _until(present)
    finally:
        stop.set()
        await task
    (entry,) = await directory.search(licence, 5)
    assert entry.provider_id == created.json()["id"] and entry.name == "Grace Hopper"


async def test_a_deactivated_login_reaches_the_assistants_deny_list(db_session, db_settings,
                                                                    factory):
    from datetime import UTC, datetime

    from app.db.redis import create_redis_client
    from app.modules.identity.models import User
    from app.modules.identity.security import hash_password
    from app.modules.identity.service import deactivate_user
    from app.modules.identity.sessions import RedisSessionStore

    email = f"pb-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, password_hash=hash_password("x"), role=UserRole.PATIENT,
                is_active=True)
    db_session.add(user)
    await db_session.commit()
    redis = create_redis_client(db_settings)
    try:
        await deactivate_user(
            db_session, email=email, sessions=RedisSessionStore(redis, db_settings),
            now=datetime.now(UTC),
        )
    finally:
        await redis.aclose()
    await _drain(db_settings, factory)

    stores = InMemoryStores()
    settings = AssistantSettings(
        kafka_bootstrap_servers=db_settings.kafka_bootstrap_servers,
        schema_registry_url=db_settings.schema_registry_url,
        topic_prefix=db_settings.resource_prefix, kafka_group=f"test-{uuid.uuid4().hex[:8]}",
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consume_revocations(settings, stores, stop))
    try:
        async def revoked():
            return await stores.revoked_at(str(user.id)) is not None

        await _until(revoked)
    finally:
        stop.set()
        await task
