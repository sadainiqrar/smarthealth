"""Temporal's SDK metrics carry a per-process tag (system health spec §3).

They come from the SDK's Rust core with no process identity of their own, so after a
restart a series came back with the same labels and Prometheus could not tell the new
count from the old one -- runs between the two were lost from `increase()`.
"""

import os
import subprocess
import sys

import pytest

from app.core.telemetry import process_id
from app.core.temporal import telemetry_config
from app.settings import Settings

pytestmark = pytest.mark.unit


def test_no_endpoint_means_no_temporal_telemetry():
    assert telemetry_config(Settings(otel_endpoint_grpc="")) is None


def test_every_process_tags_its_temporal_metrics_with_its_own_identity():
    config = telemetry_config(Settings(otel_endpoint_grpc="http://otel-collector:4317"))
    assert config.global_tags["smarthealth_process"] == process_id()


def test_the_process_identity_is_new_on_every_start():
    """In a container the app is PID 1 and the hostname survives an in-place restart, so
    host + pid repeats across restarts. A per-start token must make it differ: the same
    identity would let a restarted counter continue the old series and hide runs."""
    host, pid, token = process_id().rsplit("-", 2)
    assert pid == str(os.getpid()) and len(token) == 8
    other = subprocess.run(
        [sys.executable, "-c", "from app.core.telemetry import process_id; print(process_id())"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert other.rsplit("-", 1)[1] != token


def test_a_forked_child_gets_its_own_identity():
    """Celery forks its children after importing app modules: an identity computed at
    import would be shared by both children and merge their series."""
    if not hasattr(os, "fork"):
        pytest.skip("no fork on this platform")
    parent = process_id()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child
        os.write(write, process_id().encode())
        os._exit(0)
    os.waitpid(pid, 0)
    child = os.read(read, 200).decode()
    assert child != parent and child.split("-")[-2] == str(pid)
