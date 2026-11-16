"""Every container that can enqueue a Celery task is told where RabbitMQ is.

`Settings.rabbitmq_host` defaults to `localhost`, which is right on a laptop and wrong in
every container. A process that imports `app.celery_app` can enqueue (the consumers queue
notifications, the Temporal worker queues reminders), and one started without
`SMARTHEALTH_RABBITMQ_*` fails each enqueue against its own loopback. Nothing surfaces it:
`notifications.service._after_commit` logs a warning and Beat's sweep re-sends the message
a minute or two late -- found on a live run on 2026-11-16, where the `consumers` and
`worker` services both lacked the settings.

Which processes can enqueue is derived, not listed: each Part A service's entry module is
imported in a fresh interpreter, and if `app.celery_app` ends up loaded the service must
carry the settings.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.infra.yml"
REQUIRED = (
    "SMARTHEALTH_RABBITMQ_HOST",
    "SMARTHEALTH_RABBITMQ_PORT",
    "SMARTHEALTH_RABBITMQ_USER",
    "SMARTHEALTH_RABBITMQ_PASSWORD",
)

pytestmark = pytest.mark.meta


def _entry_module(command: list[str]) -> str | None:
    """`python -m app.x` -> `app.x`; `celery -A app.celery_app ...` -> `app.celery_app`."""
    if command[:2] == ["python", "-m"] and command[2].startswith("app."):
        return command[2]
    if command[:1] == ["celery"] and "-A" in command:
        return command[command.index("-A") + 1]
    return None


def _part_a_services() -> dict[str, tuple[str, dict]]:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    found = {}
    for name, spec in services.items():
        command = spec.get("command")
        if name == "app":
            # The API's command is the image's entrypoint (uvicorn app.main:app).
            found[name] = ("app.main", spec)
        elif isinstance(command, list) and (module := _entry_module(command)):
            found[name] = (module, spec)
    return found


def _loads_celery(module: str) -> bool:
    probe = f"import sys, {module}; print('app.celery_app' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr}"
    return result.stdout.strip().splitlines()[-1] == "True"


SERVICES = _part_a_services()


def test_the_part_a_processes_are_found():
    # A guard that finds nothing passes vacuously; these must always be among them.
    assert {"app", "worker", "consumers", "relay", "celery-worker", "celery-beat"} <= set(SERVICES)


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_a_service_that_can_enqueue_is_told_where_rabbitmq_is(service):
    module, spec = SERVICES[service]
    if not _loads_celery(module):
        pytest.skip(f"{module} never loads app.celery_app")
    environment = spec.get("environment") or {}
    missing = [name for name in REQUIRED if name not in environment]
    assert not missing, (
        f"compose service {service!r} runs {module}, which can enqueue Celery tasks, but "
        f"does not set {missing}: every enqueue would go to localhost inside the container"
    )
