"""The assistant process: the gRPC server and the revoked-sessions consumer.

    python -m assistant_service.main      (from services/assistant)

`identity.sessions_revoked` (Part A, emitted when a login is deactivated) fills the
deny-list in the assistant's own Redis, so a revoked user's still-unexpired token is
refused here within about a second -- without this service ever reading Part A's sessions.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import grpc
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from assistant_service import metrics
from assistant_service.agent import Agent
from assistant_service.auth import TokenVerifier
from assistant_service.conversations import InMemoryConversations, PostgresConversations
from assistant_service.reports import InMemoryReports, PostgresReports
from assistant_service.usage import InMemoryUsage, PostgresUsage, UsageStore
from assistant_service.llm import DEFAULT_MODELS, chat_model
from assistant_service.parta import PartA
from assistant_service.server import SERVING, build_server
from assistant_service.settings import AssistantSettings
from assistant_service.stores import RedisStores, Stores
from assistant_service.telemetry import setup as setup_tracing

logger = logging.getLogger("assistant")


async def apply_revocation(stores: Stores, value: dict) -> None:
    payload = value["payload"]
    revoked_at = payload["revoked_at"]
    await stores.revoke(
        str(payload["user_id"]),
        revoked_at.timestamp() if hasattr(revoked_at, "timestamp") else float(revoked_at),
    )


async def apply_booking(usage: UsageStore, value: dict) -> None:
    """appointments.booked -> one row per appointment (idempotent: redelivery is a no-op)."""
    payload = value["payload"]
    occurred = value["occurred_at"]
    if not isinstance(occurred, datetime):
        occurred = datetime.fromtimestamp(float(occurred) / 1000, UTC)
    origin = payload.get("origin") or "unknown"
    await usage.booking(
        str(payload["appointment_id"]), origin, payload.get("conversation_ref"), occurred
    )
    metrics.record_booking(origin)


async def consume_revocations(settings: AssistantSettings, stores: Stores, stop: asyncio.Event):
    await consume(
        settings,
        "identity.sessions_revoked",
        "revocations",
        lambda value: apply_revocation(stores, value),
        stop,
    )


async def consume_bookings(settings: AssistantSettings, usage: UsageStore, stop: asyncio.Event):
    await consume(
        settings,
        "appointments.booked",
        "conversions",
        lambda value: apply_booking(usage, value),
        stop,
    )


async def consume(
    settings: AssistantSettings, name: str, group: str, handle, stop: asyncio.Event
) -> None:
    """At-least-once: commit only after the handler succeeded; handlers are idempotent."""
    topic = settings.topic(name)
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": f"{settings.kafka_group}.{group}",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    decode = AvroDeserializer(SchemaRegistryClient({"url": settings.schema_registry_url}))
    context = SerializationContext(topic, MessageField.VALUE)
    try:
        while not stop.is_set():
            message = await asyncio.to_thread(consumer.poll, 1.0)
            if message is None or message.error():
                continue
            await handle(decode(message.value(), context))
            await asyncio.to_thread(consumer.commit, message=message, asynchronous=False)
    finally:
        await asyncio.to_thread(consumer.close)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = AssistantSettings()
    # Before the channel is made: the client instrumentation wraps channels created after it.
    tracing = setup_tracing(settings.otel_endpoint_http, settings.environment)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stores = RedisStores(redis, turn_ttl=settings.turn_ttl_seconds)
    channel = grpc.aio.insecure_channel(settings.knowledge_target)
    parta = PartA(settings.parta_url, timeout=settings.parta_timeout_seconds)
    async with (
        checkpointer(settings.database_url) as saver,
        conversation_store(settings.database_url) as (conversations, usage, reports),
    ):
        model = chat_model(
            settings.llm_mode,
            provider=settings.llm_provider,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
        logger.info("assistant model: %s", settings.llm_mode if model else "off (offline brain)")
        server = build_server(
            stores,
            TokenVerifier(settings.jwks_url, settings.jwt_issuer),
            channel,
            settings.grpc_port,
            agent=Agent(saver),
            parta=parta,
            model=model,
            timezone=ZoneInfo(settings.display_timezone),
            conversations=conversations,
            usage=usage,
            model_label=model_label(settings, model),
            prices=(settings.llm_price_input_per_mtok, settings.llm_price_output_per_mtok),
            reports=reports,
        )
        await serve(settings, server, stores, redis, usage)
    await parta.aclose()
    await channel.close()
    await redis.aclose()
    if tracing:
        tracing.shutdown()


@asynccontextmanager
async def checkpointer(database_url: str):
    """LangGraph checkpoints in the service's own Postgres, or in memory when none is set."""
    if not database_url:
        yield MemorySaver()
        return
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()  # idempotent: creates or migrates its tables
        yield saver


def model_label(settings: AssistantSettings, model) -> tuple[str, str]:
    """What the usage page names: the live provider and model, or the mode without one."""
    if (
        model is None
        or settings.llm_mode == "fixture"
        or (settings.llm_mode == "auto" and not settings.llm_api_key)
    ):
        return ("fixture", "recorded") if model is not None else ("none", "none")
    return settings.llm_provider, settings.llm_model or DEFAULT_MODELS.get(
        settings.llm_provider, ""
    )


@asynccontextmanager
async def conversation_store(database_url: str):
    """Transcripts in the same database as the checkpoints, or in memory when none is set."""
    if not database_url:
        yield InMemoryConversations(), InMemoryUsage(), InMemoryReports()
        return
    async with AsyncConnectionPool(
        database_url, min_size=1, max_size=5, open=False, kwargs={"autocommit": True}
    ) as pool:
        store, usage, reports = (
            PostgresConversations(pool),
            PostgresUsage(pool),
            PostgresReports(pool),
        )
        await store.setup()
        await usage.setup()
        await reports.setup()
        # A report that was running when the process stopped will never finish: say so.
        if failed := await reports.fail_running():
            logger.warning("%s reports were interrupted by a restart", failed)
        yield store, usage, reports


async def serve(
    settings: AssistantSettings, server, stores: Stores, redis: Redis, usage: UsageStore
) -> None:
    await server.start()
    await redis.ping()
    await server.health.set("", SERVING)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break
    logger.info("assistant serving on %s", settings.grpc_port)
    revocations = asyncio.create_task(consume_revocations(settings, stores, stop))
    conversions = asyncio.create_task(consume_bookings(settings, usage, stop))
    await stop.wait()
    await server.stop(5)
    await revocations
    await conversions


if __name__ == "__main__":
    asyncio.run(main())
