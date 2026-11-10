"""In-memory telemetry for the whole test suite, installed on import.

Imported by tests/conftest.py *before* the application, so it is the first install in
the process and therefore the one that wins (app/core/telemetry.py): the application's
own `setup_telemetry` then finds these providers and reuses them, and every span or
metric any test produces lands here, where a test can read it.
"""

from __future__ import annotations

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import telemetry

SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader()

INSTALLED = telemetry.install(
    telemetry.build_telemetry(
        "smarthealth-tests",
        "test",
        span_processors=[SimpleSpanProcessor(SPAN_EXPORTER)],
        metric_readers=[METRIC_READER],
    )
)
