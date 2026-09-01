import json

import pytest

from tests.harness.stack import ServiceStatus, TestStack, parse_ps_output

pytestmark = pytest.mark.unit

PS_JSON_LINES = "\n".join([
    json.dumps({"Service": "postgres", "State": "running", "Health": "healthy"}),
    json.dumps({"Service": "kafka", "State": "running", "Health": "starting"}),
    json.dumps({"Service": "redis", "State": "exited", "Health": ""}),
])


def test_parse_ps_output_handles_json_lines():
    statuses = parse_ps_output(PS_JSON_LINES)
    assert statuses == [
        ServiceStatus("postgres", "running", "healthy"),
        ServiceStatus("kafka", "running", "starting"),
        ServiceStatus("redis", "exited", ""),
    ]


def test_parse_ps_output_handles_a_json_array():
    array = json.dumps([
        {"Service": "postgres", "State": "running", "Health": "healthy"},
    ])
    assert parse_ps_output(array) == [ServiceStatus("postgres", "running", "healthy")]


def test_parse_ps_output_handles_empty_input():
    assert parse_ps_output("") == []
    assert parse_ps_output("\n  \n") == []


def test_service_is_ready_when_healthy_or_running_without_a_healthcheck():
    assert ServiceStatus("postgres", "running", "healthy").ready
    assert ServiceStatus("jaeger", "running", "").ready
    assert not ServiceStatus("kafka", "running", "starting").ready
    assert not ServiceStatus("redis", "exited", "").ready


def test_up_command_targets_the_test_project():
    stack = TestStack(project="smarthealth-test", env_file=".env.test",
                      compose_file="docker-compose.infra.yml")
    assert stack.compose_command("up", "-d", "--wait") == [
        "docker", "compose",
        "-p", "smarthealth-test",
        "--env-file", ".env.test",
        "-f", "docker-compose.infra.yml",
        "up", "-d", "--wait",
    ]


def test_unhealthy_services_are_reported(monkeypatch):
    stack = TestStack()
    monkeypatch.setattr(stack, "_run", lambda *args, **kwargs: PS_JSON_LINES)
    assert [status.service for status in stack.unhealthy()] == ["kafka", "redis"]
