"""Remove one test run's Kafka resources from the shared test stack.

A run namespaces everything it creates with its prefix (`t_<run>_<worker>_`, see
`isolation.py`): topics and their dead-letter topics, Schema Registry subjects
(`<topic>-value`) and consumer groups (`<prefix>group.<consumer>`). Nothing removed them, so
the test stack's Kafka volume grew without bound (41.7 GB by 2026-11-18). This removes them
when the session ends, beside the dropped database. The broker's short retention in
`.env.test` is the safety net for a run that dies before it gets here.

Best effort by design: cleanup must never fail a test run, so errors are logged, not raised.
"""

from __future__ import annotations

import logging

from confluent_kafka.admin import AdminClient

from app.events import kafka
from app.settings import Settings

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 30


def remove_run_resources(settings: Settings) -> None:
    """Delete the topics, subjects and consumer groups under `settings.resource_prefix`."""
    prefix = settings.resource_prefix
    if not prefix.startswith("t_"):  # never touch anything but a test run's namespace
        raise ValueError(f"refusing to clean a non-test prefix: {prefix!r}")
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})
    _delete_topics(admin, prefix)
    _delete_groups(admin, prefix)
    _delete_subjects(settings, prefix)


def _delete_topics(admin: AdminClient, prefix: str) -> None:
    try:
        topics = [t for t in admin.list_topics(timeout=TIMEOUT_SECONDS).topics
                  if t.startswith(prefix)]
        for name, future in (admin.delete_topics(topics) if topics else {}).items():
            try:
                future.result(timeout=TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - one topic must not stop the rest
                logger.warning("could not delete topic %s: %s", name, exc)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning("topic cleanup for %s failed: %s", prefix, exc)


def _delete_groups(admin: AdminClient, prefix: str) -> None:
    try:
        listed = admin.list_consumer_groups().result(timeout=TIMEOUT_SECONDS)
        groups = [g.group_id for g in listed.valid if g.group_id.startswith(prefix)]
        for name, future in (admin.delete_consumer_groups(groups) if groups else {}).items():
            try:
                future.result(timeout=TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete consumer group %s: %s", name, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("consumer group cleanup for %s failed: %s", prefix, exc)


def _delete_subjects(settings: Settings, prefix: str) -> None:
    try:
        client = kafka.schema_registry(settings)
        for subject in [s for s in client.get_subjects() if s.startswith(prefix)]:
            client.delete_subject(subject)                  # soft delete, then
            client.delete_subject(subject, permanent=True)  # gone for good
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema subject cleanup for %s failed: %s", prefix, exc)
