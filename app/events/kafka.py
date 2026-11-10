"""Kafka and Schema Registry clients, topic setup, and Avro (de)serialisation.

Every topic name goes through `Settings.topic()` (harness requirement 1); every schema
subject is `<namespaced topic>-value`, Schema Registry's default naming, so a run's
subjects are namespaced along with its topics.
"""

from __future__ import annotations

import logging
from typing import Any

from confluent_kafka import Consumer, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from confluent_kafka.schema_registry import Schema, SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import MessageField, SerializationContext

from app.events import catalog, schemas
from app.settings import Settings

logger = logging.getLogger(__name__)

#: Every schema evolves under this rule (spec D-22): a consumer on the newest schema can
#: read every event ever written, which compacted topics replayed from offset 0 need.
COMPATIBILITY = "BACKWARD_TRANSITIVE"

ADMIN_TIMEOUT_SECONDS = 15


def broker_topic(settings: Settings, name: str) -> str:
    return settings.topic(name)


def subject(settings: Settings, name: str) -> str:
    return f"{broker_topic(settings, name)}-value"


def schema_registry(settings: Settings) -> SchemaRegistryClient:
    return SchemaRegistryClient({"url": settings.schema_registry_url})


def producer(settings: Settings, **overrides: Any) -> Producer:
    """An idempotent producer: the broker drops a retried duplicate, and `acks=all`
    means "delivered" is reported only once the write is durable."""
    config = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "enable.idempotence": True,
        "acks": "all",
        "linger.ms": 5,
        "client.id": f"{settings.service_name}-relay",
    }
    config.update(overrides)
    return Producer(config)


def consumer(settings: Settings, group: str, **overrides: Any) -> Consumer:
    """A consumer that commits offsets itself, after the handler's transaction commits
    -- never automatically, which could commit an event nobody applied."""
    config = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": f"{settings.kafka_group}.{group}",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "enable.partition.eof": False,
    }
    config.update(overrides)
    return Consumer(config)


def ensure_topics(settings: Settings) -> list[str]:
    """Create every catalogued topic and its dead-letter topic, if missing. Idempotent.

    Topic auto-creation is off on the broker: compaction and partition counts must be
    declared, and a misspelt topic must fail loudly rather than appear silently.
    """
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})
    existing = set(admin.list_topics(timeout=ADMIN_TIMEOUT_SECONDS).topics)
    wanted: list[NewTopic] = []
    for entry in catalog.TOPICS.values():
        for name, config in (
            (entry.name, catalog.topic_config(entry)),
            (entry.dead_letter, catalog.topic_config(catalog.Topic(entry.dead_letter,
                                                                   catalog.TopicKind.EVENT))),
        ):
            namespaced = broker_topic(settings, name)
            if namespaced not in existing:
                wanted.append(NewTopic(namespaced, num_partitions=entry.partitions,
                                       replication_factor=1, config=config))
    created: list[str] = []
    if wanted:
        for name, future in admin.create_topics(wanted).items():
            try:
                future.result(timeout=ADMIN_TIMEOUT_SECONDS)
                created.append(name)
            except KafkaException as exc:
                if "TOPIC_ALREADY_EXISTS" not in str(exc):
                    raise
    return created


def register_schemas(settings: Settings) -> dict[str, int]:
    """Set every subject's compatibility rule and register its current schema.

    Registering is idempotent (the same schema returns the same id). A schema that breaks
    compatibility is refused by Schema Registry here, at deploy time, instead of at the
    first publish.
    """
    client = schema_registry(settings)
    ids: dict[str, int] = {}
    for name in catalog.TOPICS:
        name_subject = subject(settings, name)
        try:
            client.set_compatibility(subject_name=name_subject, level=COMPATIBILITY)
        except SchemaRegistryError:
            # The subject does not exist yet on some registry versions; register first.
            pass
        ids[name] = client.register_schema(name_subject, Schema(schemas.schema_text(name), "AVRO"))
        client.set_compatibility(subject_name=name_subject, level=COMPATIBILITY)
    return ids


def serializer(settings: Settings, client: SchemaRegistryClient, name: str) -> AvroSerializer:
    # auto.register off: schemas are registered (and compatibility-checked) by
    # `register_schemas` at deploy time; a serializer that registered on the fly would
    # let a breaking schema in through the first publish.
    return AvroSerializer(
        client, schemas.schema_text(name),
        conf={"auto.register.schemas": False, "use.latest.version": False},
    )


def deserializer(client: SchemaRegistryClient) -> AvroDeserializer:
    """Reads any of our topics: the writer's schema comes from the message's schema id."""
    return AvroDeserializer(client)


def value_context(settings: Settings, name: str) -> SerializationContext:
    return SerializationContext(broker_topic(settings, name), MessageField.VALUE)
