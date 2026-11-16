"""Live demo activity: real people doing real things through Part A's HTTP API.

Every booking here starts a real `BookAppointmentWorkflow`, writes real events and sends real
(Mailpit) emails -- so a reviewer finds workflow histories, traces and messages, not just
rows. Bookings are spread over two front-desk logins and the linked patients, and a 429 is
waited out (bookings are limited per user per minute).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    STAFF_LOGINS,
    UPCOMING_DAYS,
    Network,
    is_child,
    patient_identity,
)

TARGET_UPCOMING = 20
TARGET_WAITLIST = 2
WALK_IN_START = 600  # walk-in patients booked by the front desk come from here on
_CONFIRM_TIMEOUT = 60.0


@dataclass
class LiveSummary:
    booked: int = 0
    cancelled: int = 0
    rescheduled: int = 0
    waitlisted: int = 0


class LiveClient:
    def __init__(self, http: httpx.AsyncClient, password: str) -> None:
        self._http = http
        self._password = password
        self._tokens: dict[str, dict[str, str]] = {}

    async def headers(self, email: str) -> dict[str, str]:
        if email not in self._tokens:
            response = await self._http.post(
                "/auth/login", json={"email": email, "password": self._password})
            response.raise_for_status()
            self._tokens[email] = {"Authorization":
                                   f"Bearer {response.json()['access_token']}"}
        return self._tokens[email]

    async def _send(self, method: str, url: str, email: str, **kwargs) -> httpx.Response:
        extra = kwargs.pop("extra_headers", {})  # popped once: a retry must resend it
        for _ in range(6):
            response = await self._http.request(
                method, url, headers={**(await self.headers(email)), **extra}, **kwargs)
            if response.status_code != 429:
                return response
            await asyncio.sleep(int(response.headers.get("Retry-After", "5")))
        return response

    async def free_slots(self, email: str, provider_id: uuid.UUID, start: datetime,
                         end: datetime) -> list[dict]:
        response = await self._send("GET", "/slots", email, params={
            "provider_id": str(provider_id), "from": start.isoformat(), "to": end.isoformat(),
            "limit": 200})
        response.raise_for_status()
        return response.json()["items"]

    async def book(self, email: str, patient_id: uuid.UUID, slot_id: str) -> str:
        response = await self._send(
            "POST", "/appointments", email,
            json={"patient_id": str(patient_id), "slot_id": slot_id,
                  "reason": "Booked for the demo"},
            extra_headers={"Idempotency-Key": f"demo-{uuid.uuid4()}"})
        response.raise_for_status()
        return response.json()["appointment_id"]

    async def wait(self, email: str, appointment_id: str) -> str:
        deadline = asyncio.get_running_loop().time() + _CONFIRM_TIMEOUT
        while True:
            response = await self._send("GET", f"/appointments/{appointment_id}", email)
            response.raise_for_status()
            status = response.json()["status"]
            if status != "pending":
                return status
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"appointment {appointment_id} still pending")
            await asyncio.sleep(0.5)

    async def cancel(self, email: str, appointment_id: str, reason: str) -> None:
        response = await self._send("POST", f"/appointments/{appointment_id}/cancel", email,
                                    json={"reason": reason})
        response.raise_for_status()

    async def reschedule(self, email: str, appointment_id: str, new_slot_id: str) -> str:
        response = await self._send(
            "POST", f"/appointments/{appointment_id}/reschedule", email,
            json={"new_slot_id": new_slot_id, "reason": "Patient asked to move it"},
            extra_headers={"Idempotency-Key": f"demo-{uuid.uuid4()}"})
        response.raise_for_status()
        return response.json()["appointment_id"]

    async def join_waitlist(self, email: str, patient_id: uuid.UUID, provider_id: uuid.UUID,
                            start: datetime, end: datetime) -> None:
        response = await self._send("POST", "/waitlist", email, json={
            "patient_id": str(patient_id), "provider_id": str(provider_id),
            "window_start": start.isoformat(), "window_end": end.isoformat()})
        response.raise_for_status()


async def run_live(http: httpx.AsyncClient, net: Network, *, password: str, now: datetime,
                   target: int = TARGET_UPCOMING, existing_upcoming: int = 0,
                   existing_waitlist: int = 0) -> LiveSummary:
    """Book up to `target` upcoming appointments, then cancel one and reschedule one of the
    new bookings, and top the waitlist up to `TARGET_WAITLIST` entries."""
    client = LiveClient(http, password)
    summary = LiveSummary()
    desks = [email for email, role in STAFF_LOGINS if role.value == "front_desk"]
    adult_providers = [p for p in PROVIDERS if p.specialty != "Paediatrics"]
    start, end = now + timedelta(days=1), now + timedelta(days=UPCOMING_DAYS)
    walk_ins = [i for i in range(WALK_IN_START, PATIENT_COUNT) if not is_child(i)]

    bookers: list[tuple[str, int]] = [
        (patient_identity(i).email, i) for i in LINKED_PATIENT_INDEXES]
    bookers += [(desks[n % len(desks)], walk_ins[(existing_upcoming + n) % len(walk_ins)])
                for n in range(target)]
    made: list[tuple[str, uuid.UUID]] = []  # (appointment id, provider id)
    for n, (email, patient_index) in enumerate(bookers[:max(0, target - existing_upcoming)]):
        provider = adult_providers[n % len(adult_providers)]
        provider_id = net.provider_ids[provider.licence]
        slots = await client.free_slots(email, provider_id, start, end)
        if not slots:
            continue
        slot = slots[(n * 3) % len(slots)]
        appointment_id = await client.book(email, net.patient_ids[patient_index], slot["id"])
        if await client.wait(email, appointment_id) == "confirmed":
            summary.booked += 1
            made.append((appointment_id, provider_id))

    if len(made) >= 3:
        await client.cancel(desks[0], made[0][0], "Patient called to cancel")
        summary.cancelled = 1
        appointment_id, provider_id = made[1]
        slots = await client.free_slots(desks[0], provider_id, start, end)
        if slots:
            new_id = await client.reschedule(desks[0], appointment_id, slots[-1]["id"])
            if await client.wait(desks[0], new_id) == "confirmed":
                summary.rescheduled = 1

    for n in range(max(0, TARGET_WAITLIST - existing_waitlist)):
        provider = adult_providers[(n + 5) % len(adult_providers)]
        await client.join_waitlist(
            desks[1 % len(desks)], net.patient_ids[walk_ins[-(n + 1)]],
            net.provider_ids[provider.licence], start, now + timedelta(days=7))
        summary.waitlisted += 1
    return summary
