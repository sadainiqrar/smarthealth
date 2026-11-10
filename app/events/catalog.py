"""Every topic: its logical name, kind, schema version and configuration.

The logical name (`appointments.cancelled`) is what code and schemas use; the topic on
the broker is `Settings.topic(name)`, namespaced by the run's prefix (harness requirement
1). Adding a topic means a row here and a schema in contracts/events/ -- a meta-test
holds the two in step.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TopicKind(enum.Enum):
    #: Things that happened. Time-retained (7 days); a consumer further behind than that
    #: has lost events, which consumer-lag alerting exists to prevent.
    EVENT = "event"
    #: The current state of an entity, keyed by its id. Log-compacted: the latest value
    #: per key is kept forever, so a brand-new consumer -- a Part B service -- builds a
    #: complete copy by reading from offset 0.
    STATE = "state"


@dataclass(frozen=True)
class Topic:
    name: str
    kind: TopicKind
    schema_version: int = 1
    partitions: int = 3

    @property
    def dead_letter(self) -> str:
        return f"{self.name}.dlt"


RETENTION_MS = 7 * 24 * 60 * 60 * 1000

TOPICS: dict[str, Topic] = {
    entry.name: entry
    for entry in (
        Topic("appointments.booked", TopicKind.EVENT),
        Topic("appointments.cancelled", TopicKind.EVENT),
        Topic("appointments.rescheduled", TopicKind.EVENT),
        Topic("appointments.no_show", TopicKind.EVENT),
        Topic("visits.completed", TopicKind.EVENT),
        Topic("identity.sessions_revoked", TopicKind.EVENT),
        Topic("providers.state", TopicKind.STATE),
        Topic("clinics.state", TopicKind.STATE),
        Topic("departments.state", TopicKind.STATE),
    )
}


def topic(name: str) -> Topic:
    try:
        return TOPICS[name]
    except KeyError:
        raise KeyError(f"no topic named {name!r} in app/events/catalog.py") from None


def topic_config(entry: Topic) -> dict[str, str]:
    """Broker configuration for a topic. Dead-letter topics use the event settings."""
    if entry.kind is TopicKind.STATE:
        # min.compaction.lag keeps a just-written value visible to slow consumers before
        # the cleaner may collapse it; the tombstone retention lets a consumer that was
        # down for a day still see a deletion.
        return {
            "cleanup.policy": "compact",
            "min.compaction.lag.ms": str(60 * 60 * 1000),
            "delete.retention.ms": str(24 * 60 * 60 * 1000),
        }
    return {"cleanup.policy": "delete", "retention.ms": str(RETENTION_MS)}
