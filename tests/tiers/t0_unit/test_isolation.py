import re

import pytest

from tests.harness.isolation import make_isolation

pytestmark = pytest.mark.unit

POSTGRES_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def test_same_run_id_and_worker_produce_the_same_names():
    first = make_isolation(run_id="ab12cd34", worker_id="gw0")
    second = make_isolation(run_id="ab12cd34", worker_id="gw0")
    assert first == second


def test_different_run_ids_produce_different_names():
    first = make_isolation(run_id="ab12cd34", worker_id="master")
    second = make_isolation(run_id="ef56gh78", worker_id="master")
    assert first.postgres_db != second.postgres_db
    assert first.kafka_topic_prefix != second.kafka_topic_prefix


def test_workers_share_a_run_but_get_distinct_redis_databases():
    master = make_isolation(run_id="ab12cd34", worker_id="master")
    gw0 = make_isolation(run_id="ab12cd34", worker_id="gw0")
    gw1 = make_isolation(run_id="ab12cd34", worker_id="gw1")
    assert {master.redis_db, gw0.redis_db, gw1.redis_db} == {0, 1, 2}
    assert master.postgres_db != gw0.postgres_db


def test_postgres_database_name_is_a_legal_identifier():
    isolation = make_isolation(run_id="ab12cd34", worker_id="gw3")
    assert POSTGRES_IDENTIFIER.match(isolation.postgres_db)


def test_resource_prefix_namespaces_topics_queues_and_task_queues():
    isolation = make_isolation(run_id="ab12cd34", worker_id="gw0")
    assert isolation.kafka_topic_prefix == isolation.resource_prefix
    assert isolation.temporal_task_queue_prefix == isolation.resource_prefix
    assert isolation.resource_prefix.startswith("t_ab12cd34_gw0_")


def test_run_id_is_generated_when_absent(monkeypatch):
    monkeypatch.delenv("SMARTHEALTH_TEST_RUN_ID", raising=False)
    isolation = make_isolation()
    assert len(isolation.run_id) == 8
    assert isolation.run_id.isalnum()


def test_run_id_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SMARTHEALTH_TEST_RUN_ID", "deadbeef")
    assert make_isolation().run_id == "deadbeef"


def test_env_maps_every_resource_setting():
    isolation = make_isolation(run_id="ab12cd34", worker_id="gw0")
    env = isolation.as_env()
    assert env["SMARTHEALTH_RESOURCE_PREFIX"] == isolation.resource_prefix
    assert env["SMARTHEALTH_POSTGRES_DB"] == isolation.postgres_db
    assert isinstance(env["SMARTHEALTH_REDIS_DB"], str)
