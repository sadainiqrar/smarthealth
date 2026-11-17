"""The knowledge service's metrics (system health spec §3).

From the global meter: no-ops until `telemetry.setup` installs a provider, so tests and an
un-exported process pay nothing. Prometheus sees `knowledge_searches_total{outcome}` and
`knowledge_search_duration_milliseconds`.

Rejected calls (bad token, empty query) and cancelled calls are not counted; `error` means
the search itself failed.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("knowledge")

searches = _meter.create_counter(
    "knowledge.searches", unit="1",
    description="Searches by outcome: hit, no_results, error",
)
search_duration = _meter.create_histogram(
    "knowledge.search.duration", unit="ms", description="One Search call, start to reply",
)


def record_search(outcome: str, elapsed_ms: float) -> None:
    searches.add(1, {"outcome": outcome})
    search_duration.record(elapsed_ms, {"outcome": outcome})
