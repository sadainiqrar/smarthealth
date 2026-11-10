"""Part A's HTTP API, as the assistant reaches it: always with the *user's* token.

Part A's authorisation is the real gate (spec 9.2): the assistant has no authority of its
own, so a prompt that talks the model into asking for someone else's record meets the same
403/404 the user would. This client only adds what a dependent service owes its callers:

- **timeouts** on every call, so a slow Part A cannot hold a chat turn open;
- **a circuit breaker**: after `failure_threshold` consecutive failures (connection errors
  or 5xx) calls fail fast for `reset_seconds`, then one probe is let through. While it is
  open the assistant says it cannot reach the booking system and still answers
  knowledge questions (degradation, spec 9.2);
- **idempotent writes**: a booking carries `Idempotency-Key` (derived from the
  confirmation it answers) and `X-Conversation-Ref`, so a retried confirm returns the same
  appointment and the booking is attributed to the conversation (PART-B-FR-5).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class PartAUnavailable(Exception):
    """Part A could not be reached, or the breaker is open."""


class PartAError(Exception):
    """Part A answered, and said no (4xx)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class Breaker:
    failure_threshold: int = 5
    reset_seconds: float = 15.0
    failures: int = 0
    opened_at: float | None = None

    def allow(self, now: float) -> bool:
        if self.opened_at is None:
            return True
        return now - self.opened_at >= self.reset_seconds  # half-open: one probe

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = now


class PartA:
    """One per process; each call names the token it acts with."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: Breaker | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self.breaker = breaker or Breaker()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        now = time.monotonic()
        if not self.breaker.allow(now):
            raise PartAUnavailable("the booking system is not responding")
        merged = {"Authorization": f"Bearer {token}", **(headers or {})}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = await self._http.request(
                method, path, params=clean, json=json, headers=merged
            )
        except httpx.HTTPError as exc:
            self.breaker.failure(time.monotonic())
            raise PartAUnavailable(str(exc) or type(exc).__name__) from exc
        if response.status_code >= 500:
            self.breaker.failure(time.monotonic())
            raise PartAUnavailable(f"Part A answered {response.status_code}")
        self.breaker.success()
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise PartAError(response.status_code, str(detail))
        return response.json() if response.content else None

    # -- reads ------------------------------------------------------------------------

    async def me_patient(self, token: str) -> dict:
        return await self.request("GET", "/patients/me", token)

    async def providers(
        self,
        token: str,
        *,
        specialty: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        page = await self.request(
            "GET",
            "/providers",
            token,
            params={"specialty": specialty, "search": search, "limit": limit},
        )
        return [p for p in page["items"] if p.get("is_active", True)]

    async def provider(self, token: str, provider_id: str) -> dict:
        return await self.request("GET", f"/providers/{provider_id}", token)

    async def slots(
        self,
        token: str,
        provider_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        page = await self.request(
            "GET",
            "/slots",
            token,
            params={"provider_id": provider_id, "from": start, "to": end, "limit": limit},
        )
        return page["items"]

    async def appointments(self, token: str, **filters: Any) -> list[dict]:
        params = {"limit": 50, **filters}
        if "from_" in params:
            params["from"] = params.pop("from_")
        page = await self.request("GET", "/appointments", token, params=params)
        return page["items"]

    async def appointment(self, token: str, appointment_id: str) -> dict:
        return await self.request("GET", f"/appointments/{appointment_id}", token)

    async def my_record(self, token: str) -> dict:
        return await self.request("GET", "/patients/me/record", token)

    async def record(self, token: str, patient_id: str) -> dict:
        return await self.request("GET", f"/patients/{patient_id}/record", token)

    async def document(self, token: str, patient_id: str, document_id: str) -> dict:
        return await self.request("GET", f"/patients/{patient_id}/documents/{document_id}", token)

    async def patients(self, token: str, search: str, limit: int = 10) -> list[dict]:
        page = await self.request(
            "GET", "/patients", token, params={"search": search, "limit": limit}
        )
        return page["items"]

    async def patient(self, token: str, patient_id: str) -> dict:
        return await self.request("GET", f"/patients/{patient_id}", token)

    async def waitlist(self, token: str, **filters: Any) -> list[dict]:
        page = await self.request("GET", "/waitlist", token, params={"limit": 50, **filters})
        return page["items"]

    async def analytics_summary(self, token: str, **params: Any) -> dict:
        return await self.request("GET", "/analytics/summary", token, params=params)

    # -- writes (each only after the user confirmed, spec 9.2) --------------------------

    def _write_headers(self, idempotency_key: str, conversation_ref: str) -> dict[str, str]:
        return {"Idempotency-Key": idempotency_key, "X-Conversation-Ref": conversation_ref}

    async def book(
        self,
        token: str,
        *,
        patient_id: str,
        slot_id: str,
        reason: str | None,
        idempotency_key: str,
        conversation_ref: str,
    ) -> dict:
        return await self.request(
            "POST",
            "/appointments",
            token,
            json={"patient_id": patient_id, "slot_id": slot_id, "reason": reason},
            headers=self._write_headers(idempotency_key, conversation_ref),
        )

    async def cancel(self, token: str, appointment_id: str, reason: str) -> dict:
        return await self.request(
            "POST", f"/appointments/{appointment_id}/cancel", token, json={"reason": reason}
        )

    async def reschedule(
        self,
        token: str,
        appointment_id: str,
        *,
        new_slot_id: str,
        idempotency_key: str,
        conversation_ref: str,
    ) -> dict:
        return await self.request(
            "POST",
            f"/appointments/{appointment_id}/reschedule",
            token,
            json={"new_slot_id": new_slot_id},
            headers=self._write_headers(idempotency_key, conversation_ref),
        )

    async def join_waitlist(
        self, token: str, *, patient_id: str, provider_id: str, window_start: str, window_end: str
    ) -> dict:
        return await self.request(
            "POST",
            "/waitlist",
            token,
            json={
                "patient_id": patient_id,
                "provider_id": provider_id,
                "window_start": window_start,
                "window_end": window_end,
            },
        )

    async def send_message(self, token: str, *, patient_id: str, subject: str, body: str,
                           idempotency_key: str) -> dict:
        """An approved message, through Part A's notifications (spec 9.5)."""
        return await self.request(
            "POST", "/notifications", token,
            json={"patient_id": patient_id, "subject": subject, "body": body},
            headers={"Idempotency-Key": idempotency_key})

    async def note(self, token: str, appointment_id: str) -> dict:
        return await self.request("GET", f"/appointments/{appointment_id}/note", token)

    async def save_note(self, token: str, appointment_id: str, body: dict) -> dict:
        return await self.request("PUT", f"/appointments/{appointment_id}/note", token,
                                  json=body)
