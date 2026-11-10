"""The payment gateway client: HTTP, idempotency keys, a timeout and a circuit breaker.

Every call carries an `Idempotency-Key` naming the operation and the appointment
(`authorize-<id>`, `capture-<id>`, ...). A Temporal activity retried after the gateway
answered -- the worker died, the commit failed, the response timed out -- sends the same key
and gets the same answer, so nothing is charged twice (spec D-27).

Failures split two ways, and the split decides what Temporal does:

- `GatewayUnavailable` (a 5xx, a timeout, a refused connection, an open circuit): retryable.
  The activity is retried with backoff; the booking waits, it does not fail.
- `GatewayRejected` (a 4xx): permanent. Retrying the same request cannot change the answer.

A *decline* is neither: it is a normal answer (`status: declined`), recorded, and acted on
by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.circuit import CircuitBreaker
from app.core.errors import ServiceUnavailable, Unprocessable


class GatewayUnavailable(ServiceUnavailable):
    """The gateway could not be reached or failed; retrying later may work."""


class GatewayRejected(Unprocessable):
    """The gateway refused the request itself; retrying cannot help."""


@dataclass(frozen=True)
class GatewayResult:
    id: str
    status: str
    decline_reason: str | None = None


class PaymentGateway:
    def __init__(
        self, base_url: str, *, timeout_seconds: float = 2.0,
        breaker: CircuitBreaker | None = None, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._breaker = breaker or CircuitBreaker("payment gateway")
        self._transport = transport

    async def _post(
        self, path: str, *, key: str, json: dict[str, Any] | None = None
    ) -> GatewayResult:
        self._breaker.before_call()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(path, json=json, headers={"Idempotency-Key": key})
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise GatewayUnavailable(f"payment gateway unreachable: {exc!r}") from exc
        if response.status_code >= 500:
            self._breaker.record_failure()
            raise GatewayUnavailable(f"payment gateway answered {response.status_code}")
        self._breaker.record_success()
        if response.status_code >= 400:
            raise GatewayRejected(
                f"payment gateway refused {path}: {response.status_code} {response.text[:200]}"
            )
        body = response.json()
        return GatewayResult(body["id"], body["status"], body.get("decline_reason"))

    async def authorize(
        self, *, key: str, amount_minor: int, currency: str, customer_ref: str, reference: str
    ) -> GatewayResult:
        return await self._post("/authorizations", key=key, json={
            "amount_minor": amount_minor, "currency": currency,
            "customer_ref": customer_ref, "reference": reference,
        })

    async def capture(self, authorization_id: str, *, key: str) -> GatewayResult:
        return await self._post(f"/authorizations/{authorization_id}/capture", key=key)

    async def void(self, authorization_id: str, *, key: str) -> GatewayResult:
        return await self._post(f"/authorizations/{authorization_id}/void", key=key)

    async def refund(self, authorization_id: str, *, key: str) -> GatewayResult:
        return await self._post("/refunds", key=key, json={"authorization_id": authorization_id})
