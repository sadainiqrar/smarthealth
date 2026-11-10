"""A small in-memory Part A for the assistant's contract tests (httpx.MockTransport).

It answers only the routes the assistant calls, with Part A's shapes, and records every
write with its headers -- so a test can say "one booking, with this Idempotency-Key and this
conversation ref". It is not Part A's rules: who may do what is proven against the real
Part A (T3, test_assistant_against_part_a.py); this fake proves the assistant's side.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass
class FakePartA:
    patient: dict = field(
        default_factory=lambda: {
            "id": str(uuid.uuid4()),
            "mrn": "1042",
            "first_name": "Sam",
            "last_name": "Patel",
            "date_of_birth": "1990-04-02",
            "user_id": "u-patient",
        }
    )
    providers: list[dict] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)
    appointments: dict[str, dict] = field(default_factory=dict)
    record: dict = field(default_factory=dict)
    writes: list[tuple[str, str, dict, dict]] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    #: appointment id -> its clinical note (GET/PUT /appointments/{id}/note).
    notes: dict[str, dict] = field(default_factory=dict)
    #: Idempotency-Keys of staff messages already queued (POST /notifications).
    sent_keys: set[str] = field(default_factory=set)
    #: Query parameters of every GET /analytics/summary, in order.
    analytics_calls: list[dict] = field(default_factory=list)
    down: bool = False
    #: Status a new booking ends in ("confirmed", or "failed" to simulate a decline).
    outcome: str = "confirmed"

    @classmethod
    def seeded(cls) -> FakePartA:
        fake = cls()
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        for first, last, specialty, user in (
            ("Ada", "Lovelace", "Cardiology", "u-ada"),
            ("Grace", "Hopper", "Cardiology", "u-grace"),
            ("Alan", "Turing", "Dermatology", "u-alan"),
        ):
            pid = str(uuid.uuid4())
            fake.providers.append(
                {
                    "id": pid,
                    "first_name": first,
                    "last_name": last,
                    "specialty": specialty,
                    "license_number": "L",
                    "is_active": True,
                    "user_id": user,
                }
            )
            for hours in (26, 50, 74):
                start = now + timedelta(hours=hours + len(fake.providers))
                fake.slots.append(
                    {
                        "id": str(uuid.uuid4()),
                        "provider_id": pid,
                        "clinic_id": str(uuid.uuid4()),
                        "department_id": None,
                        "starts_at": _iso(start),
                        "ends_at": _iso(start + timedelta(minutes=30)),
                        "status": "free",
                    }
                )
        fake.record = {
            "patient_id": fake.patient["id"],
            "notes": [],
            "documents": [],
            "prescriptions": [
                {
                    "id": str(uuid.uuid4()),
                    "medication": "Amoxicillin",
                    "dose": "500 mg",
                    "frequency": "three times a day",
                    "duration": "7 days",
                    "instructions": "Take with food. Finish the course.",
                    "status": "active",
                }
            ],
            "orders": [
                {
                    "id": str(uuid.uuid4()),
                    "test_code": "fasting-glucose",
                    "test_name": "Fasting glucose",
                    "prep_source_id": "doc:prep-blood-tests#fasting-blood-test",
                    "urgency": "routine",
                    "status": "ordered",
                    "created_at": _iso(now - timedelta(days=1)),
                    "reason": None,
                    "result_document_id": None,
                }
            ],
        }
        return fake

    # -- the transport --------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Part A is down", request=request)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"detail": "not authenticated"})
        path, q = request.url.path, request.url.params
        body = json.loads(request.content) if request.content else {}
        method = request.method
        if method != "GET":
            self.writes.append((method, path, body, dict(request.headers)))
        if (method, path) == ("GET", "/patients/me"):
            return httpx.Response(200, json=self.patient)
        if (method, path) == ("GET", "/patients"):
            term = q.get("search", "").lower()
            hit = (
                term
                in (
                    self.patient["mrn"] + self.patient["first_name"] + self.patient["last_name"]
                ).lower()
            )
            return self._page([self.patient] if hit else [])
        if method == "GET" and path == f"/patients/{self.patient['id']}":
            return httpx.Response(200, json=self.patient)
        if (method, path) in (
            ("GET", "/patients/me/record"),
            ("GET", f"/patients/{self.patient['id']}/record"),
        ):
            return httpx.Response(200, json=self.record)
        if (method, path) == ("GET", "/providers"):
            items = [
                p
                for p in self.providers
                if not q.get("specialty") or p["specialty"] == q.get("specialty")
            ]
            return self._page(items)
        if method == "GET" and path.startswith("/providers/"):
            found = [p for p in self.providers if p["id"] == path.rsplit("/", 1)[1]]
            return (
                httpx.Response(200, json=found[0])
                if found
                else httpx.Response(404, json={"detail": "no"})
            )
        if (method, path) == ("GET", "/slots"):
            items = [
                s
                for s in self.slots
                if s["provider_id"] == q.get("provider_id") and s["status"] == "free"
            ]
            return self._page(
                sorted(items, key=lambda s: s["starts_at"])[: int(q.get("limit", 20))]
            )
        if (method, path) == ("POST", "/appointments"):
            return self._book(body, request.headers)
        if (method, path) == ("GET", "/appointments"):
            return self._page(sorted(self.appointments.values(), key=lambda a: a["starts_at"]))
        if method == "GET" and path.startswith("/appointments/") and not path.endswith("/note"):
            found = self.appointments.get(path.rsplit("/", 1)[1])
            return (
                httpx.Response(200, json=found)
                if found
                else httpx.Response(404, json={"detail": "no"})
            )
        if method == "POST" and path.endswith("/cancel"):
            appointment = self.appointments[path.split("/")[2]]
            appointment["status"] = "cancelled"
            self._slot(appointment["slot_id"])["status"] = "free"
            return httpx.Response(200, json=appointment)
        if path.endswith("/note") and path.startswith("/appointments/"):
            appointment_id = path.split("/")[2]
            if method == "GET":
                found = self.notes.get(appointment_id)
                return (
                    httpx.Response(200, json=found)
                    if found
                    else httpx.Response(404, json={"detail": "no note"})
                )
            note = self.notes[appointment_id]
            if note["status"] != "draft":
                return httpx.Response(409, json={"detail": "a signed note is not edited"})
            note.update(body)
            return httpx.Response(200, json=note)
        if (method, path) == ("GET", "/analytics/summary"):
            self.analytics_calls.append(dict(q))
            busy = len(self.analytics_calls) % 2 == 1  # this period first, the one before second
            return httpx.Response(
                200,
                json={
                    "start": q.get("from"),
                    "end": q.get("to"),
                    "total_patients": 120,
                    "appointments_booked": 48 if busy else 40,
                    "completed_visits": 30 if busy else 31,
                    "cancellations": 6 if busy else 3,
                    "no_shows": 2,
                    "cancellation_rate": 0.125 if busy else 0.075,
                    "average_wait_seconds": 540.0,
                },
            )
        if (method, path) == ("GET", "/analytics/utilisation"):
            return httpx.Response(
                200,
                json={
                    "start": q.get("from"),
                    "end": q.get("to"),
                    "departments": [
                        {
                            "clinic_id": "c1",
                            "clinic": "North",
                            "department_id": "d1",
                            "department": "Cardiology",
                            "slots": 40,
                            "booked": 34,
                            "utilisation": 0.85,
                        },
                        {
                            "clinic_id": "c1",
                            "clinic": "North",
                            "department_id": "d2",
                            "department": "Dermatology",
                            "slots": 30,
                            "booked": 6,
                            "utilisation": 0.2,
                        },
                    ],
                },
            )
        if (method, path) == ("GET", "/analytics/timeseries"):
            return httpx.Response(
                200,
                json={"metric": q.get("metric"), "points": [{"day": q.get("from"), "value": 7.0}]},
            )
        if (method, path) == ("POST", "/notifications"):
            key = request.headers["idempotency-key"]
            if key in self.sent_keys:
                return httpx.Response(202, json={"notification_id": None, "status": "duplicate"})
            self.sent_keys.add(key)
            return httpx.Response(
                202, json={"notification_id": str(uuid.uuid4()), "status": "queued"}
            )
        if (method, path) == ("POST", "/waitlist"):
            return httpx.Response(
                201,
                json={
                    "id": str(uuid.uuid4()),
                    **body,
                    "department_id": None,
                    "status": "active",
                    "created_at": _iso(datetime.now(UTC)),
                },
            )
        return httpx.Response(404, json={"detail": f"fake Part A has no {method} {path}"})

    def _page(self, items: list[dict]) -> httpx.Response:
        return httpx.Response(
            200, json={"items": items, "total": len(items), "limit": 50, "offset": 0}
        )

    def _slot(self, slot_id: str) -> dict:
        return next(s for s in self.slots if s["id"] == slot_id)

    def _book(self, body: dict, headers: httpx.Headers) -> httpx.Response:
        key = headers.get("idempotency-key")
        if key in self.keys:
            return httpx.Response(202, json={"appointment_id": self.keys[key], "status": "pending"})
        slot = self._slot(body["slot_id"])
        if slot["status"] != "free":
            return httpx.Response(409, json={"detail": "that slot is no longer free"})
        slot["status"] = "booked"
        appointment_id = str(uuid.uuid4())
        self.keys[key] = appointment_id
        self.appointments[appointment_id] = {
            "id": appointment_id,
            "status": self.outcome,
            "patient_id": body["patient_id"],
            "provider_id": slot["provider_id"],
            "slot_id": slot["id"],
            "starts_at": slot["starts_at"],
            "reason": body.get("reason"),
            "in_progress": False,
        }
        return httpx.Response(202, json={"appointment_id": appointment_id, "status": "pending"})

    @property
    def bookings(self) -> list[tuple[str, str, dict, dict]]:
        return [w for w in self.writes if w[:2] == ("POST", "/appointments")]
