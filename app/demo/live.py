"""Live demo activity: real people doing real things through Part A's HTTP API.

Every booking here starts a real `BookAppointmentWorkflow`, writes real events and sends real
(Mailpit) emails -- so a reviewer finds workflow histories, traces and messages, not just
rows. Bookings are spread over two front-desk logins and the linked patients, and a 429 is
waited out (bookings are limited per user per minute).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    WaitlistEntry,
    WaitlistStatus,
)
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

TARGET_UPCOMING = 12
TARGET_WAITLIST = 2
WALK_IN_START = 600  # walk-in patients booked by the front desk come from here on
_CONFIRM_TIMEOUT = 60.0

log = logging.getLogger(__name__)


@dataclass
class LiveState:
    """What the demo patients already hold, read from the database before `run_live`."""

    busy_patient_ids: frozenset[uuid.UUID]
    waiting_patient_ids: frozenset[uuid.UUID]
    upcoming: int  # live upcoming appointments of demo patients
    waitlist: int  # open waitlist entries of demo patients


async def live_state(session: AsyncSession, net: Network, now: datetime) -> LiveState:
    """Demo patients with a pending or confirmed appointment starting after `now`, and demo
    patients with an active waitlist entry."""
    busy = (await session.execute(
        select(Appointment.patient_id)
        .join(ProviderSlot, ProviderSlot.id == Appointment.slot_id)
        .where(Appointment.patient_id.in_(net.patient_ids),
               Appointment.provider_id.in_(net.provider_ids.values()),
               Appointment.status.in_((AppointmentStatus.PENDING,
                                       AppointmentStatus.CONFIRMED)),
               ProviderSlot.starts_at > now))).scalars().all()
    waiting = (await session.execute(
        select(WaitlistEntry.patient_id).where(
            WaitlistEntry.patient_id.in_(net.patient_ids),
            WaitlistEntry.status == WaitlistStatus.ACTIVE))).scalars().all()
    return LiveState(frozenset(busy), frozenset(waiting), len(busy), len(waiting))


@dataclass
class LiveSummary:
    booked: int = 0
    cancelled: int = 0
    rescheduled: int = 0
    waitlisted: int = 0
    failed: int = 0
    #: One line per booking or reschedule that did not confirm: what, and the outcome.
    failures: list[str] = field(default_factory=list)

    def fail(self, what: str, detail: str) -> None:
        self.failed += 1
        self.failures.append(f"{what}: {detail}")
        log.warning("demo live activity: %s did not confirm (%s)", what, detail)


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
        relogged = False
        response: httpx.Response | None = None
        for _ in range(6):
            response = await self._http.request(
                method, url, headers={**(await self.headers(email)), **extra}, **kwargs)
            if response.status_code == 401 and not relogged:
                # The session ended (expiry, a purge): log in again, once.
                relogged = True
                self._tokens.pop(email, None)
                continue
            if response.status_code != 429:
                return response
            await asyncio.sleep(int(response.headers.get("Retry-After", "5")))
        assert response is not None
        return response

    async def free_slots(self, email: str, provider_id: uuid.UUID, start: datetime,
                         end: datetime) -> list[dict]:
        response = await self._send("GET", "/slots", email, params={
            "provider_id": str(provider_id), "from": start.isoformat(), "to": end.isoformat(),
            "limit": 200})
        response.raise_for_status()
        return response.json()["items"]

    async def book(self, email: str, patient_id: uuid.UUID, slot_id: str) -> str:
        # A deterministic key (well under the 200-character header limit): booking the same
        # patient into the same slot twice is one appointment, not two.
        response = await self._send(
            "POST", "/appointments", email,
            json={"patient_id": str(patient_id), "slot_id": slot_id,
                  "reason": "Booked for the demo"},
            extra_headers={"Idempotency-Key": f"demo-{patient_id}-{slot_id}"})
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

    async def wait_while(self, email: str, appointment_id: str, status: str) -> str:
        """Poll until the appointment is no longer `status` (the old half of a reschedule
        leaves `confirmed` a moment after the new one confirms)."""
        deadline = asyncio.get_running_loop().time() + _CONFIRM_TIMEOUT
        while True:
            response = await self._send("GET", f"/appointments/{appointment_id}", email)
            response.raise_for_status()
            current = response.json()["status"]
            if current != status:
                return current
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"appointment {appointment_id} still {status}")
            await asyncio.sleep(0.5)

    async def cancel(self, email: str, appointment_id: str, reason: str) -> None:
        response = await self._send("POST", f"/appointments/{appointment_id}/cancel", email,
                                    json={"reason": reason})
        response.raise_for_status()

    async def reschedule(self, email: str, appointment_id: str, new_slot_id: str) -> str:
        response = await self._send(
            "POST", f"/appointments/{appointment_id}/reschedule", email,
            json={"new_slot_id": new_slot_id, "reason": "Patient asked to move it"},
            extra_headers={"Idempotency-Key": f"demo-resched-{appointment_id}-{new_slot_id}"})
        response.raise_for_status()
        return response.json()["appointment_id"]

    async def join_waitlist(self, email: str, patient_id: uuid.UUID, provider_id: uuid.UUID,
                            start: datetime, end: datetime) -> None:
        response = await self._send("POST", "/waitlist", email, json={
            "patient_id": str(patient_id), "provider_id": str(provider_id),
            "window_start": start.isoformat(), "window_end": end.isoformat()})
        response.raise_for_status()


async def run_live(
    http: httpx.AsyncClient, net: Network, *, password: str, now: datetime,
    target: int = TARGET_UPCOMING, existing_upcoming: int = 0, existing_waitlist: int = 0,
    busy_patient_ids: frozenset[uuid.UUID] = frozenset(),
    waiting_patient_ids: frozenset[uuid.UUID] = frozenset(),
) -> LiveSummary:
    """Book up to `target - existing_upcoming` upcoming appointments, waitlist patients for
    the provider and day of one of them, cancel it (so its release has someone to promote)
    and reschedule another.

    `busy_patient_ids` already hold a live upcoming appointment and are not booked again;
    `waiting_patient_ids` have an open waitlist entry and are not queued again. A patient is
    used at most once per run, so no one is double-booked. A booking or reschedule that does
    not confirm is recorded in `LiveSummary.failures`, never dropped."""
    client = LiveClient(http, password)
    summary = LiveSummary()
    desks = [email for email, role in STAFF_LOGINS if role.value == "front_desk"]
    adult_providers = [p for p in PROVIDERS if p.specialty != "Paediatrics"]
    start, end = now + timedelta(days=1), now + timedelta(days=UPCOMING_DAYS)
    walk_ins = [i for i in range(WALK_IN_START, PATIENT_COUNT) if not is_child(i)]
    # Rotate by what already exists, so a refresh meets new patients, providers and slots.
    rotate = existing_upcoming % len(walk_ins)
    walk_ins = walk_ins[rotate:] + walk_ins[:rotate]
    used: set[uuid.UUID] = set(busy_patient_ids)

    bookers: list[tuple[str, int]] = [
        (patient_identity(i).email, i) for i in LINKED_PATIENT_INDEXES]
    bookers += [(desks[n % len(desks)], index) for n, index in enumerate(walk_ins)]
    # (appointment id, provider id, slot start, slot end)
    made: list[tuple[str, uuid.UUID, datetime, datetime]] = []
    wanted = max(0, target - existing_upcoming)
    attempt = 0
    for email, patient_index in bookers:
        if len(made) + summary.failed >= wanted:
            break
        patient_id = net.patient_ids[patient_index]
        if patient_id in used:
            continue
        provider = adult_providers[(attempt + existing_upcoming) % len(adult_providers)]
        provider_id = net.provider_ids[provider.licence]
        attempt += 1
        slots = await client.free_slots(email, provider_id, start, end)
        if not slots:
            continue
        used.add(patient_id)
        slot = slots[(attempt * 3 + existing_upcoming) % len(slots)]
        try:
            appointment_id = await client.book(email, patient_id, slot["id"])
            outcome = await client.wait(email, appointment_id)
        except (httpx.HTTPStatusError, TimeoutError) as error:
            summary.fail(f"booking of slot {slot['id']}", repr(error))
            continue
        if outcome == "confirmed":
            summary.booked += 1
            made.append((appointment_id, provider_id,
                         datetime.fromisoformat(slot["starts_at"]),
                         datetime.fromisoformat(slot["ends_at"])))
        else:
            summary.fail(f"booking {appointment_id}", outcome)

    # The waitlist: patients waiting for the provider and the day of the appointment about
    # to be cancelled, so its release has someone to promote. With nothing to cancel they
    # wait on an adult provider's coming week.
    cancel_target = made[0] if len(made) >= 3 else None
    if cancel_target is not None:
        _, wait_provider, slot_start, slot_end = cancel_target
        window = (slot_start - timedelta(hours=1), slot_end + timedelta(hours=1))
    else:
        wait_provider = net.provider_ids[adult_providers[5 % len(adult_providers)].licence]
        window = (start, now + timedelta(days=7))
    for index in reversed(walk_ins):
        if summary.waitlisted >= max(0, TARGET_WAITLIST - existing_waitlist):
            break
        patient_id = net.patient_ids[index]
        if patient_id in used or patient_id in waiting_patient_ids:
            continue
        used.add(patient_id)
        await client.join_waitlist(desks[1 % len(desks)], patient_id, wait_provider, *window)
        summary.waitlisted += 1

    if cancel_target is not None:
        await client.cancel(desks[0], cancel_target[0], "Patient called to cancel")
        summary.cancelled = 1
        appointment_id, provider_id, _, _ = made[1]
        slots = await client.free_slots(desks[0], provider_id, start, end)
        if slots:
            try:
                new_id = await client.reschedule(desks[0], appointment_id, slots[-1]["id"])
                outcome = await client.wait(desks[0], new_id)
            except (httpx.HTTPStatusError, TimeoutError) as error:
                summary.fail(f"reschedule of {appointment_id}", repr(error))
            else:
                if outcome == "confirmed":
                    summary.rescheduled = 1
                    await client.wait_while(desks[0], appointment_id, "confirmed")
                else:
                    summary.fail(f"reschedule {new_id}", outcome)
    return summary
