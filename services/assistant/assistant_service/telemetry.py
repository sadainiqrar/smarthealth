"""Traces and metrics from the assistant to the shared OpenTelemetry Collector (D-28).

Each Part B service builds its own provider -- its own copy of this code, as with auth --
and exports to the same collector as Part A under its own service.name, so one trace runs
assistant -> knowledge and lines up with Part A's in Jaeger. gRPC is instrumented on both
sides: the server extracts the caller's trace context from metadata, the client injects it.
"""

from __future__ import annotations

import os
import socket
import time
import uuid

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.grpc import (
    GrpcAioInstrumentorClient,
    GrpcAioInstrumentorServer,
)
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


HEALTH = "/grpc.health.v1.Health/"


def TRACED(call_details) -> bool:  # noqa: N802 - a constant-like filter, named as one
    """Which incoming calls become spans: everything but the gRPC health probes, which
    compose sends every few seconds and which otherwise fill Jaeger (live run, 2026-11-16).
    Not `filters.health_check()`: it splits the method with `os.path`, which on Windows
    turns "/" into "\\" and never matches."""
    method = getattr(call_details, "method", "")
    if isinstance(method, bytes):
        method = method.decode()
    return not method.startswith(HEALTH)


#: New on every start: host and pid repeat across an in-place container restart, and a
#: restarted counter would then continue the old series and hide the runs in between.
_START_TOKEN = uuid.uuid4().hex[:8]


def setup(endpoint: str, environment: str) -> TracerProvider | None:
    """Install tracing if an endpoint is configured; otherwise do nothing at all."""
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({
        "service.name": "assistant", "service.namespace": "smarthealth",
        "deployment.environment": environment,
        # One series per process (see app/core/telemetry.py).
        "service.instance.id": f"{socket.gethostname()}-{os.getpid()}-{_START_TOKEN}",
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    # Metrics (metrics.py) on the same resource, every 15 s, for the Assistant dashboard.
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"), export_interval_millis=15_000)
    meter_provider = MeterProvider(resource=provider.resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    def beat(_options: CallbackOptions):
        yield Observation(time.time())

    # The heartbeat (system health spec §3): alive while this keeps moving.
    meter_provider.get_meter("assistant.heartbeat").create_observable_gauge(
        "smarthealth.heartbeat", callbacks=[beat], unit="s",
        description="Unix time at the process's last metric export: its liveness")
    GrpcAioInstrumentorServer(filter_=TRACED).instrument()
    GrpcAioInstrumentorClient().instrument()
    return provider
