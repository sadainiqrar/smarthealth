"""OpenTelemetry providers for one process (Weeks 3-5 spec D-28; harness requirement 5).

`build_telemetry` is pure: it returns a tracer provider and a meter provider and touches
no global. `install` makes one of them the process-global provider, and **the first
install wins** -- the test suite installs in-memory exporters before the application is
imported, and the application's own `setup_telemetry` then finds and reuses them instead
of replacing them. OpenTelemetry refuses to replace a global provider anyway; making that
rule explicit here is what keeps the providers swappable rather than accidentally fixed.

Every process exports to one OpenTelemetry Collector and nothing else; where the data
goes after that (Jaeger, Prometheus) is the collector's business, not the code's.

FastAPI instrumentation lives in `app.main`, not here: `app/core` is framework-free
(tests/meta/test_import_boundaries.py).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from app.settings import Settings

logger = logging.getLogger(__name__)

#: How often metrics leave the process. Prometheus scrapes the collector every 10 s, so
#: exporting faster would only be overwritten before anyone read it.
METRIC_EXPORT_INTERVAL_MS = 10_000


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    #: The OTLP/HTTP base URL both signals export to, or None for "nowhere".
    exports_to: str | None

    def flush(self) -> None:
        """Push buffered spans and metrics out now.

        Safe to call repeatedly, unlike `shutdown`: the API's lifespan runs once per
        contract test, always against the one process-global provider, and shutting it
        down there would silence every later test.
        """
        self.tracer_provider.force_flush()
        self.meter_provider.force_flush()

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def build_telemetry(
    service_name: str,
    environment: str,
    *,
    otlp_http_endpoint: str = "",
    span_processors: Sequence[SpanProcessor] = (),
    metric_readers: Sequence[MetricReader] = (),
) -> Telemetry:
    """Providers for one process. Exports over OTLP/HTTP only if an endpoint is given;
    `span_processors` and `metric_readers` are the test seam."""
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "smarthealth",
            "deployment.environment": environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    for processor in span_processors:
        tracer_provider.add_span_processor(processor)

    readers = list(metric_readers)
    if otlp_http_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_http_endpoint}/v1/traces"))
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{otlp_http_endpoint}/v1/metrics"),
                export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
            )
        )

    return Telemetry(
        tracer_provider=tracer_provider,
        meter_provider=MeterProvider(resource=resource, metric_readers=readers),
        exports_to=otlp_http_endpoint or None,
    )


_installed: Telemetry | None = None


def install(telemetry: Telemetry) -> Telemetry:
    """Make `telemetry` the process-global provider, unless one already is.

    Returns whichever is installed afterwards, so a caller always holds the live one.
    """
    global _installed
    if _installed is None:
        trace.set_tracer_provider(telemetry.tracer_provider)
        metrics.set_meter_provider(telemetry.meter_provider)
        _installed = telemetry
    return _installed


def setup_telemetry(settings: Settings, service_name: str) -> Telemetry:
    """Install this process's providers from settings, or return the installed ones.

    Checks first rather than building and discarding: building with an endpoint starts
    exporter threads, which a discarded provider would leak.
    """
    if _installed is not None:
        return _installed
    telemetry = install(
        build_telemetry(
            service_name, settings.environment, otlp_http_endpoint=settings.otel_endpoint_http
        )
    )
    # Celery: spans for publishing and running tasks, and the trace context carried in
    # task headers, so a consumer's span is the parent of the send task's. In every process
    # -- producers inject, workers extract.
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    instrumentor = CeleryInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()
    logger.info(
        "telemetry installed",
        extra={"service_name": service_name, "exports_to": telemetry.exports_to},
    )
    return telemetry


def instrument_engine(engine: AsyncEngine) -> None:
    """Trace every SQL statement on `engine`.

    One engine per process, by design: the instrumentor is a process-wide singleton, so
    once it is on, a later engine (the contract tier runs the API lifespan per test) is
    left as it is rather than re-instrumented with a warning each time.
    """
    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(engine=engine.sync_engine)
