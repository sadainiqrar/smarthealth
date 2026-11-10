"""Keeping the provider directory: Part A's `providers.state`, read from the beginning.

A compacted topic holds the latest value of every provider forever, so a fresh service --
or one whose database was wiped -- reads from offset 0 and has the whole directory. Each
value replaces the entry outright (state, not a delta), so a redelivered or replayed value
writes the same row again: idempotent by construction. A tombstone (no value) removes it.
Offsets are committed after the write.
"""

from __future__ import annotations

import asyncio
import logging

from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

from knowledge_service.settings import KnowledgeSettings
from knowledge_service.store import Directory, entry_from_state

logger = logging.getLogger(__name__)


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return str(value) if value.__class__.__name__ == "UUID" else value


async def apply(directory: Directory, key: str, value: dict | None) -> None:
    if value is None:
        await directory.remove(key)
        return
    payload = _plain(value["payload"])
    await directory.upsert(entry_from_state(payload), list(payload.get("department_ids", [])))


async def run(settings: KnowledgeSettings, directory: Directory, stop: asyncio.Event) -> None:
    topic = settings.topic("providers.state")
    consumer = Consumer({
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": f"{settings.kafka_group}.provider-directory",
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
            value = decode(message.value(), context) if message.value() else None
            await apply(directory, (message.key() or b"").decode(), value)
            await asyncio.to_thread(consumer.commit, message=message, asynchronous=False)
    finally:
        await asyncio.to_thread(consumer.close)
