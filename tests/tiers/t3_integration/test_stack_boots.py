import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker]

EXPECTED_SERVICES = {
    "postgres", "mongo", "redis", "rabbitmq", "kafka", "schema-registry", "temporal",
}


def test_every_infrastructure_service_reports_healthy(stack):
    statuses = stack.status()
    assert {status.service for status in statuses} >= EXPECTED_SERVICES
    unhealthy = [f"{s.service}={s.state}/{s.health or 'no-healthcheck'}" for s in stack.unhealthy()]
    assert not unhealthy, f"services not ready: {', '.join(unhealthy)}"
