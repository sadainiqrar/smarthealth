"""Every process says it is alive, every export interval, whether or not it is busy
(system health spec §3): an idle process otherwise exports nothing and looks dead."""

import time

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.core.telemetry import process_id, build_telemetry, register_heartbeat

pytestmark = pytest.mark.unit


def _points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    return [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def test_the_heartbeat_reports_the_current_unix_time():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-test", "test", metric_readers=[reader])
    register_heartbeat(telemetry.meter_provider)
    before = time.time()
    points = _points(reader, "smarthealth.heartbeat")
    assert len(points) == 1
    assert before - 1 <= points[0].value <= time.time() + 1


def test_registering_twice_on_one_provider_keeps_one_series():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-test", "test", metric_readers=[reader])
    register_heartbeat(telemetry.meter_provider)
    register_heartbeat(telemetry.meter_provider)
    assert len(_points(reader, "smarthealth.heartbeat")) == 1


def test_each_process_gets_its_own_series_identity():
    import os

    telemetry = build_telemetry("part-a-test", "test")
    instance = telemetry.tracer_provider.resource.attributes["service.instance.id"]
    assert str(os.getpid()) in instance
    assert instance == process_id()  # host-pid-token: new on every start
