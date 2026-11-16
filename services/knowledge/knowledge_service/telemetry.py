"""Traces from the knowledge service to the shared OpenTelemetry Collector (spec D-28).

Each Part B service builds its own provider -- its own copy of this code, as with auth --
and exports to the same collector as Part A under its own service.name, so one trace runs
assistant -> knowledge and lines up with Part A's in Jaeger. gRPC is instrumented on both
sides: the server extracts the caller's trace context from metadata, the client injects it.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.grpc import (
    GrpcAioInstrumentorClient,
    GrpcAioInstrumentorServer,
)
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


def setup(endpoint: str, environment: str) -> TracerProvider | None:
    """Install tracing if an endpoint is configured; otherwise do nothing at all."""
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({
        "service.name": "knowledge", "service.namespace": "smarthealth",
        "deployment.environment": environment,
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    GrpcAioInstrumentorServer(filter_=TRACED).instrument()
    GrpcAioInstrumentorClient().instrument()
    return provider
