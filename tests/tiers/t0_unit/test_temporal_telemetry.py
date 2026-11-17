"""Temporal's SDK metrics carry a per-process tag (system health spec §3).

They come from the SDK's Rust core with no process identity of their own, so after a
restart a series came back with the same labels and Prometheus could not tell the new
count from the old one -- runs between the two were lost from `increase()`.
"""

import os

import pytest

from app.core.temporal import telemetry_config
from app.settings import Settings

pytestmark = pytest.mark.unit


def test_no_endpoint_means_no_temporal_telemetry():
    assert telemetry_config(Settings(otel_endpoint_grpc="")) is None


def test_every_process_tags_its_temporal_metrics_with_its_own_identity():
    config = telemetry_config(Settings(otel_endpoint_grpc="http://otel-collector:4317"))
    process = config.global_tags["smarthealth_process"]
    assert process.endswith(f"-{os.getpid()}")
