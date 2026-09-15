"""The running process must emit JSON, not merely own a JSON formatter.

Four T0 tests already assert that `JsonFormatter` formats a record and that
`configure_logging` installs it idempotently. All four passed for the whole of Week 1
while the served application emitted no JSON whatsoever: uvicorn sets
`propagate = False` on `uvicorn` and `uvicorn.access`, so the one log line a request
produces never reached the root handler those tests were inspecting.

The subject of a test has to match the subject of the claim. "The formatter formats" is
not "the service emits structured logs" — the distance between them is a third-party
library's logging configuration, which no unit test touches. So this one starts a real
server in a real subprocess and reads its real output.

It lives in the contract tier because that tier's defining property is "no containers",
and this needs none: `/health` touches no infrastructure and every client is lazy.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.contract

STARTUP_TIMEOUT_SECONDS = 30.0


def _free_port() -> int:
    """Ask the OS for a port, then release it.

    Racy in principle. The alternative — a hardcoded port — fails whenever a previous
    run is still shutting down, which is the more likely collision in practice.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _parse_json_lines(output: str) -> list[dict]:
    records = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


@pytest.fixture(scope="module")
def served_output() -> str:
    """Run the app under uvicorn, make one request, return everything it printed.

    Module-scoped: booting a real interpreter, importing the application and waiting
    for the port costs seconds, and all three assertions below read the same output.

    `-u` is not optional: without it the child buffers stdout when it is a pipe, and
    the assertions below would be reading an empty string rather than the log.
    """
    port = _free_port()
    environment = os.environ | {"SMARTHEALTH_LOG_LEVEL": "INFO", "PYTHONUNBUFFERED": "1"}

    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "app.main:app", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(f"server exited early:\n{process.communicate()[0]}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                process.kill()
                pytest.fail(f"server never became reachable:\n{process.communicate()[0]}")
            time.sleep(0.1)
    finally:
        process.terminate()

    return process.communicate(timeout=STARTUP_TIMEOUT_SECONDS)[0]


def test_the_request_log_line_is_json(served_output: str) -> None:
    """The regression guard proper.

    Before the fix this failed on a line reading
    `INFO:     127.0.0.1:56042 - "GET /health HTTP/1.1" 200 OK` — uvicorn's
    `AccessFormatter` output, which is not JSON and never passed through ours.
    """
    access_lines = [line for line in served_output.splitlines() if "GET /health" in line]
    assert access_lines, f"no access log line was emitted at all:\n{served_output}"

    for line in access_lines:
        assert line.strip().startswith("{"), (
            f"access log line is not JSON: {line!r}\n\nfull output:\n{served_output}"
        )
        json.loads(line)


def test_the_access_record_carries_the_expected_fields(served_output: str) -> None:
    """A log aggregator indexes fields; assert the fields exist to be indexed."""
    access = [
        record
        for record in _parse_json_lines(served_output)
        if record.get("logger") == "uvicorn.access"
    ]
    assert access, f"no uvicorn.access record reached the JSON handler:\n{served_output}"

    record = access[0]
    assert set(record) >= {"timestamp", "level", "logger", "message"}
    assert record["level"] == "INFO"
    assert "GET /health" in record["message"]


def test_uvicorn_own_lifecycle_logs_are_json_once_the_app_has_started(
    served_output: str,
) -> None:
    """`uvicorn.error` is taken over too, not just the access log.

    Scoped deliberately to *"Application startup complete."*: the lines before it
    ("Started server process", "Waiting for application startup.") are emitted before
    the lifespan runs, so they are still uvicorn-formatted and this test must not
    pretend otherwise.
    """
    matches = [
        line for line in served_output.splitlines() if "Application startup complete" in line
    ]
    assert matches, f"server never logged startup completion:\n{served_output}"

    for line in matches:
        assert line.strip().startswith("{"), (
            f"lifecycle log line is not JSON: {line!r}\n\nfull output:\n{served_output}"
        )
