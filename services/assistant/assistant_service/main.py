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

import grpc
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from redis.asyncio import Redis

from assistant_service.auth import TokenVerifier
from assistant_service.server import SERVING, build_server
from assistant_service.settings import AssistantSettings
from assistant_service.stores import RedisStores, Stores
from assistant_service.telemetry import setup as setup_tracing

logger = logging.getLogger("assistant")


async def apply_revocation(stores: Stores, value: dict) -> None:
    payload = value["payload"]
    revoked_at = payload["revoked_at"]
    await stores.revoke(str(payload["user_id"]), revoked_at.timestamp()
                        if hasattr(revoked_at, "timestamp") else float(revoked_at))


async def consume_revocations(settings: AssistantSettings, stores: Stores, stop: asyncio.Event):
    topic = settings.topic("identity.sessions_revoked")
    consumer = Consumer({
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": f"{settings.kafka_group}.revocations",
        "enable.auto.commit": False, "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic])
    decode = AvroDeserializer(SchemaRegistryClient({"url": settings.schema_registry_url}))
    context = SerializationContext(topic, MessageField.VALUE)
    try:
        while not stop.is_set():
            message = await asyncio.to_thread(consumer.poll, 1.0)
            if message is None or message.error():
                continue
            await apply_revocation(stores, decode(message.value(), context))
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
    server = build_server(stores, TokenVerifier(settings.jwks_url, settings.jwt_issuer),
                          channel, settings.grpc_port)
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
    await stop.wait()
    await server.stop(5)
    await revocations
    await channel.close()
    await redis.aclose()
    if tracing:
        tracing.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
