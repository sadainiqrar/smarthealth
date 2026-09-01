"""Compose test-stack lifecycle.

Readiness comes from each service's own healthcheck via `docker compose up --wait`,
never from a sleep. `TestStack` also exposes per-service controls, which the chaos
tier builds on.

The API is deliberately testcontainers-shaped: if ephemeral containers are ever
needed, this module is the only one that changes.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

DEFAULT_PROJECT = "smarthealth-test"
DEFAULT_ENV_FILE = ".env.test"
DEFAULT_COMPOSE_FILE = "docker-compose.infra.yml"
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ServiceStatus:
    service: str
    state: str
    health: str

    @property
    def ready(self) -> bool:
        """Healthy, or running with no healthcheck declared."""
        if self.state != "running":
            return False
        return self.health in ("healthy", "")


def parse_ps_output(raw: str) -> list[ServiceStatus]:
    """Parse `docker compose ps --format json`, which emits JSON lines or a JSON array."""
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [
        ServiceStatus(
            service=record.get("Service", ""),
            state=record.get("State", ""),
            health=record.get("Health", "") or "",
        )
        for record in records
    ]


class TestStack:
    """Brings the shared infrastructure stack up, reports on it, and tears it down."""

    def __init__(
        self,
        project: str = DEFAULT_PROJECT,
        env_file: str = DEFAULT_ENV_FILE,
        compose_file: str = DEFAULT_COMPOSE_FILE,
    ) -> None:
        self.project = project
        self.env_file = env_file
        self.compose_file = compose_file

    def compose_command(self, *args: str) -> list[str]:
        return [
            "docker", "compose",
            "-p", self.project,
            "--env-file", self.env_file,
            "-f", self.compose_file,
            *args,
        ]

    def _run(self, *args: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
        completed = subprocess.run(
            self.compose_command(*args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"`{' '.join(self.compose_command(*args))}` failed "
                f"({completed.returncode}):\n{completed.stderr.strip()}"
            )
        return completed.stdout

    def up(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Start every service and block until each reports healthy."""
        self._run("up", "-d", "--wait", timeout=timeout)

    def down(self, remove_volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"]
        if remove_volumes:
            args.append("--volumes")
        self._run(*args)

    def status(self) -> list[ServiceStatus]:
        return parse_ps_output(self._run("ps", "--format", "json", "--all"))

    def unhealthy(self) -> list[ServiceStatus]:
        return [status for status in self.status() if not status.ready]

    def is_running(self) -> bool:
        statuses = self.status()
        return bool(statuses) and all(status.ready for status in statuses)

    # --- per-service controls; the chaos tier builds on these ---

    def kill(self, service: str) -> None:
        self._run("kill", service)

    def pause(self, service: str) -> None:
        self._run("pause", service)

    def unpause(self, service: str) -> None:
        self._run("unpause", service)

    def restart(self, service: str) -> None:
        self._run("restart", service)
