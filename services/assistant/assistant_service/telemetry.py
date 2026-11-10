"""Traces from the assistant service to the shared OpenTelemetry Collector (spec D-28).

Each Part B service builds its own provider -- its own copy of this code, as with auth --
and exports to the same collector as Part A under its own service.name, so one trace runs
assistant -> knowledge and lines up with Part A's in Jaeger. gRPC is instrumented on both
sides: the server extracts the caller's trace context from metadata, the client injects it.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient, GrpcAioInstrumentorServer
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup(endpoint: str, environment: str) -> TracerProvider | None:
    """Install tracing if an endpoint is configured; otherwise do nothing at all."""
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({
        "service.name": "assistant", "service.namespace": "smarthealth",
        "deployment.environment": environment,
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    GrpcAioInstrumentorServer().instrument()
    GrpcAioInstrumentorClient().instrument()
    return provider
