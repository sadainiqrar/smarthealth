"""Thin clients for the two monitoring sources (system health spec §3).

Both raise `SourceUnavailable` for anything other than a usable answer, so the service has
one thing to catch and the page one thing to say: "metrics unavailable".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import httpx

TIMEOUT_SECONDS = 3.0


class SourceUnavailable(Exception):
    """The source did not answer, or refused the question."""


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


class PrometheusClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def query(self, promql: str) -> list[Sample]:
        """An instant query: one value per series, now."""
        try:
            response = await self._http.get(
                f"{self._base}/api/v1/query", params={"query": promql}, timeout=TIMEOUT_SECONDS)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"prometheus: {exc}") from exc
        if (response.status_code != 200 or not isinstance(body, dict)
                or body.get("status") != "success"):
            detail = body.get("error") if isinstance(body, dict) else None
            raise SourceUnavailable(f"prometheus: {detail or response.status_code}")
        try:
            return [
                Sample(labels=dict(r["metric"]), value=float(r["value"][1]))
                for r in body["data"]["result"]
            ]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise SourceUnavailable(f"prometheus: unexpected answer ({exc!r})") from exc


class JaegerClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def error_traces(
        self, service: str, start: datetime, end: datetime, limit: int
    ) -> list[dict]:
        """Traces of `service` holding a span tagged error=true, newest first as Jaeger
        returns them. This is the API Jaeger's own UI uses (spec §10: pinned to 1.62)."""
        params = {
            "service": service,
            "tags": json.dumps({"error": "true"}, separators=(",", ":")),
            "start": str(int(start.timestamp() * 1_000_000)),
            "end": str(int(end.timestamp() * 1_000_000)),
            "limit": str(limit),
        }
        try:
            response = await self._http.get(
                f"{self._base}/api/traces", params=params, timeout=TIMEOUT_SECONDS)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"jaeger: {exc}") from exc
        if response.status_code != 200:
            raise SourceUnavailable(f"jaeger: HTTP {response.status_code}")
        if not isinstance(body, dict):
            raise SourceUnavailable("jaeger: unexpected answer")
        return body.get("data") or []

    async def ping(self) -> None:
        """Is Jaeger answering at all? Raises `SourceUnavailable` if not."""
        try:
            response = await self._http.get(f"{self._base}/api/services", timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"jaeger: {exc}") from exc
        if response.status_code != 200:
            raise SourceUnavailable(f"jaeger: HTTP {response.status_code}")
