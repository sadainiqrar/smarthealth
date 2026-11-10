"""Re-publishing dead letters once their cause is fixed (`python -m app.cli dlt-replay`).

A dead letter keeps the exact bytes that were sent (`value_b64`), so replay re-publishes
what the producer wrote -- not a re-encoding that might differ -- to the original topic,
with its key. The consumer that failed then sees it as a new delivery; its dedupe mark
was never written (the failing attempts rolled it back), so it applies.
"""

from __future__ import annotations

import base64
import json
import logging

from app.events import catalog, kafka
from app.settings import Settings

logger = logging.getLogger(__name__)

IDLE_POLLS_BEFORE_DONE = 5


def replay(settings: Settings, topic: str, *, limit: int | None = None) -> int:
    """Re-publish up to `limit` dead letters of `topic`; returns how many."""
    catalog.topic(topic)  # an unknown name fails here, not as an empty replay
    source = kafka.broker_topic(settings, f"{topic}.dlt")
    consumer = kafka.consumer(settings, "dlt-replay")
    consumer.subscribe([source])
    producer = kafka.producer(settings)
    replayed = idle = 0
    try:
        while idle < IDLE_POLLS_BEFORE_DONE and (limit is None or replayed < limit):
            message = consumer.poll(1.0)
            if message is None:
                idle += 1
                continue
            if message.error():
                continue
            idle = 0
            body = json.loads(message.value())
            producer.produce(
                kafka.broker_topic(settings, topic),
                key=body["key"].encode(),
                value=base64.b64decode(body["value_b64"]),
            )
            producer.flush(10)
            consumer.commit(message=message, asynchronous=False)
            replayed += 1
            logger.info("dead letter replayed", extra={"topic": topic, "offset": body["offset"]})
    finally:
        consumer.close()
    return replayed
