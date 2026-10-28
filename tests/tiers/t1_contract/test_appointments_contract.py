"""The booking endpoint's HTTP contract, with Temporal faked. No database is touched:
these paths authorise by role before any query runs, and the GET tests replace the
session with one that finds no row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.api.deps import get_session, get_temporal
from app.main import app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()
CALLER = "11111111-1111-1111-1111-111111111111"
BODY = {"patient_id": str(uuid.uuid4()), "slot_id": str(uuid.uuid4())}


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=CALLER, role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


class FakeTemporal:
    def __init__(self, error: Exception | None = None) -> None:
        self.starts: list[dict] = []
        self._error = error

    async def start_workflow(self, *args, **kwargs):
        if self._error:
            raise self._error
        self.starts.append(kwargs)


@pytest.fixture
def temporal():
    fake = FakeTemporal()
    app.dependency_overrides[get_temporal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_temporal, None)


async def test_booking_requires_a_token(api_client, temporal):
    assert (await api_client.post("/appointments", json=BODY)).status_code == 401


async def test_a_provider_may_not_book(api_client, temporal):
    response = await api_client.post("/appointments", json=BODY, headers=auth(UserRole.PROVIDER))
    assert response.status_code == 403


async def test_front_desk_booking_is_accepted_not_created(api_client, temporal):
    """202, not 201: the appointment is pending until the workflow confirms it."""
    response = await api_client.post(
        "/appointments", json=BODY, headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert temporal.starts[0]["id"] == f"booking-{response.json()['appointment_id']}"


async def test_the_same_idempotency_key_names_the_same_appointment(api_client, temporal):
    headers = {**auth(UserRole.FRONT_DESK), "Idempotency-Key": "abc"}
    first = await api_client.post("/appointments", json=BODY, headers=headers)
    second = await api_client.post("/appointments", json=BODY, headers=headers)
    assert first.json()["appointment_id"] == second.json()["appointment_id"]


async def test_temporal_down_is_a_503_in_the_standard_envelope(api_client):
    fake = FakeTemporal(RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    app.dependency_overrides[get_temporal] = lambda: fake
    try:
        response = await api_client.post(
            "/appointments", json=BODY, headers=auth(UserRole.ADMIN)
        )
    finally:
        app.dependency_overrides.pop(get_temporal, None)
    assert response.status_code == 503
    assert response.json()["error"] == "ServiceUnavailable"


async def test_reading_requires_a_token(api_client):
    assert (await api_client.get(f"/appointments/{uuid.uuid4()}")).status_code == 401


class _NoRowSession:
    """Stands in for the request session: every lookup finds nothing. Overriding
    `get_session` (rather than patching the service) keeps the real router and service
    code on the path under test."""

    async def scalar(self, statement):
        return None


class _Description:
    def __init__(self, status: WorkflowExecutionStatus) -> None:
        self.status = status


class _Handle:
    def __init__(self, outcome) -> None:
        self._outcome = outcome

    async def describe(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Description(self._outcome)


class DescribingTemporal:
    """Answers `get_workflow_handle(...).describe()` with a fixed outcome."""

    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.described: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        self.described.append(workflow_id)
        return _Handle(self._outcome)


@pytest.fixture
def no_row():
    async def _session():
        yield _NoRowSession()

    app.dependency_overrides[get_session] = _session
    yield
    app.dependency_overrides.pop(get_session, None)


async def _read_unrecorded(api_client, outcome):
    fake = DescribingTemporal(outcome)
    app.dependency_overrides[get_temporal] = lambda: fake
    appointment_id = str(uuid.uuid4())
    try:
        response = await api_client.get(
            f"/appointments/{appointment_id}", headers=auth(UserRole.FRONT_DESK)
        )
    finally:
        app.dependency_overrides.pop(get_temporal, None)
    assert fake.described == [f"booking-{appointment_id}"]
    return appointment_id, response


async def test_a_running_booking_with_no_row_reads_as_pending(api_client, no_row):
    appointment_id, response = await _read_unrecorded(
        api_client, WorkflowExecutionStatus.RUNNING
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "pending", True)
    assert body["slot_id"] is None and body["patient_id"] is None


@pytest.mark.parametrize(
    "closed",
    [
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
        WorkflowExecutionStatus.COMPLETED,
    ],
)
async def test_a_closed_booking_with_no_row_reads_as_failed(api_client, no_row, closed):
    """It ended before it recorded the appointment. Reporting pending here would leave a
    poller waiting forever."""
    appointment_id, response = await _read_unrecorded(api_client, closed)
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "failed", False)


async def test_an_unknown_booking_is_a_404(api_client, no_row):
    _, response = await _read_unrecorded(
        api_client, RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    )
    assert response.status_code == 404


async def test_temporal_down_on_read_is_a_503_not_a_404(api_client, no_row):
    """We cannot tell whether the booking exists; a 404 would say it does not."""
    _, response = await _read_unrecorded(
        api_client, RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    )
    assert response.status_code == 503
