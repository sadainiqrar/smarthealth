"""A test run removes its own Kafka topics, Schema Registry subjects and consumer groups.

Every run namespaces its topics (`t_<run>_<worker>_`) and nothing deleted them: the test
stack's Kafka volume grew to 41.7 GB (2026-11-18). `tests/harness/kafka_cleanup.py` is
called when the session's database is dropped; this proves it removes a run's resources
and leaves another run's alone.
"""

from __future__ import annotations

import pytest
from confluent_kafka.admin import AdminClient

from app.events import kafka
from tests.harness.isolation import make_isolation
from tests.harness.kafka_cleanup import remove_run_resources
from tests.tiers.t3_integration.conftest import _test_stack_settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _topics(settings, prefix: str) -> set[str]:
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})
    return {t for t in admin.list_topics(timeout=10).topics if t.startswith(prefix)}


def _subjects(settings, prefix: str) -> set[str]:
    return {s for s in kafka.schema_registry(settings).get_subjects() if s.startswith(prefix)}


def test_a_run_removes_its_topics_and_subjects_and_only_its_own(stack):
    mine = _test_stack_settings(make_isolation(run_id="cleanup-mine"))
    other = _test_stack_settings(make_isolation(run_id="cleanup-other"))
    for settings in (mine, other):
        kafka.ensure_topics(settings)
        kafka.register_schemas(settings)
    assert _topics(mine, mine.resource_prefix) and _subjects(mine, mine.resource_prefix)

    remove_run_resources(mine)

    assert _topics(mine, mine.resource_prefix) == set()
    assert _subjects(mine, mine.resource_prefix) == set()
    assert _topics(other, other.resource_prefix)  # another run's are untouched
    remove_run_resources(other)
    assert _topics(other, other.resource_prefix) == set()


def test_cleaning_up_a_run_that_made_nothing_is_a_no_op(stack):
    remove_run_resources(_test_stack_settings(make_isolation(run_id="cleanup-empty")))
