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


class _StartedDescription:
    def __init__(self, memo: dict) -> None:
        self._memo = memo

    async def memo_value(self, key, default=None):
        return self._memo.get(key, default)


class _StartedHandle:
    def __init__(self, memo: dict) -> None:
        self._memo = memo

    async def describe(self):
        return _StartedDescription(self._memo)


class FakeTemporal:
    """Records starts and, like Temporal, keeps the memo of the *first* start under an
    id: a repeat under USE_EXISTING never replaces it. `memo` pins what describe()
    reports instead, to simulate a workflow started by an earlier, different request."""

    def __init__(self, error: Exception | None = None, memo: dict | None = None) -> None:
        self.starts: list[dict] = []
        self._error = error
        self._pinned = memo
        self._memos: dict[str, dict] = {}

    async def start_workflow(self, *args, **kwargs):
        if self._error:
            raise self._error
        self.starts.append(kwargs)
        self._memos.setdefault(kwargs["id"], kwargs.get("memo") or {})

    def get_workflow_handle(self, workflow_id: str) -> _StartedHandle:
        memo = self._pinned if self._pinned is not None else self._memos[workflow_id]
        return _StartedHandle(memo)


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


async def test_an_empty_idempotency_key_is_a_422(api_client, temporal):
    """An empty key would collapse every such request from one caller onto one id."""
    response = await api_client.post(
        "/appointments", json=BODY,
        headers={**auth(UserRole.FRONT_DESK), "Idempotency-Key": ""},
    )
    assert response.status_code == 422
    assert temporal.starts == []


async def test_a_key_reused_with_a_different_body_is_a_422(api_client):
    """The IETF Idempotency-Key draft's answer: the key names a request, and this is
    not that request. Silently answering with the first booking would let the client
    believe its second request was accepted."""
    fake = FakeTemporal(memo={"fingerprint": "the fingerprint of some other request"})
    app.dependency_overrides[get_temporal] = lambda: fake
    try:
        response = await api_client.post(
            "/appointments", json=BODY,
            headers={**auth(UserRole.FRONT_DESK), "Idempotency-Key": "abc"},
        )
    finally:
        app.dependency_overrides.pop(get_temporal, None)
    assert response.status_code == 422
    assert response.json()["error"] == "Unprocessable"


async def test_a_repeat_with_the_same_body_is_accepted_again(api_client, temporal):
    headers = {**auth(UserRole.FRONT_DESK), "Idempotency-Key": "same"}
    first = await api_client.post("/appointments", json=BODY, headers=headers)
    second = await api_client.post("/appointments", json=BODY, headers=headers)
    assert (first.status_code, second.status_code) == (202, 202)
    assert temporal.starts[0]["memo"] == temporal.starts[1]["memo"]


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


NOT_FOUND = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")


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
    """Answers `get_workflow_handle(id).describe()` with a fixed outcome per kind of
    workflow -- "booking" or "reschedule", the id's prefix -- and NOT_FOUND for any kind
    not given."""

    def __init__(self, **outcomes) -> None:
        self._outcomes = outcomes
        self.described: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        self.described.append(workflow_id)
        kind = workflow_id.split("-", 1)[0]
        return _Handle(self._outcomes.get(kind, NOT_FOUND))


@pytest.fixture
def no_row():
    async def _session():
        yield _NoRowSession()

    app.dependency_overrides[get_session] = _session
    yield
    app.dependency_overrides.pop(get_session, None)


async def _read_unrecorded(api_client, *, described: list[str], **outcomes):
    """GET an appointment with no row; `described` is which workflows it must have asked
    about, in order, as kinds ("booking", "reschedule")."""
    fake = DescribingTemporal(**outcomes)
    app.dependency_overrides[get_temporal] = lambda: fake
    appointment_id = str(uuid.uuid4())
    try:
        response = await api_client.get(
            f"/appointments/{appointment_id}", headers=auth(UserRole.FRONT_DESK)
        )
    finally:
        app.dependency_overrides.pop(get_temporal, None)
    assert fake.described == [f"{kind}-{appointment_id}" for kind in described]
    return appointment_id, response


CLOSED = [
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
    WorkflowExecutionStatus.COMPLETED,
]


async def test_a_running_booking_with_no_row_reads_as_pending(api_client, no_row):
    appointment_id, response = await _read_unrecorded(
        api_client, described=["booking"], booking=WorkflowExecutionStatus.RUNNING
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "pending", True)
    assert body["slot_id"] is None and body["patient_id"] is None


@pytest.mark.parametrize("closed", CLOSED)
async def test_a_closed_booking_with_no_row_reads_as_failed(api_client, no_row, closed):
    """It ended before it recorded the appointment. Reporting pending here would leave a
    poller waiting forever."""
    appointment_id, response = await _read_unrecorded(
        api_client, described=["booking"], booking=closed
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "failed", False)


async def test_a_reschedule_not_yet_booking_reads_as_pending(api_client, no_row):
    """I-2: right after a reschedule's 202 its child `booking-<id>` may not exist yet.
    The new appointment id the 202 handed out must read as pending, not 404."""
    appointment_id, response = await _read_unrecorded(
        api_client, described=["booking", "reschedule"],
        reschedule=WorkflowExecutionStatus.RUNNING,
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "pending", True)


@pytest.mark.parametrize("closed", CLOSED)
async def test_a_reschedule_that_closed_without_a_booking_reads_as_failed(
    api_client, no_row, closed
):
    """The reschedule ended and its child never wrote a row: the new appointment failed."""
    appointment_id, response = await _read_unrecorded(
        api_client, described=["booking", "reschedule"], reschedule=closed
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"], body["in_progress"]) == (appointment_id, "failed", False)


async def test_an_unknown_booking_or_reschedule_is_a_404(api_client, no_row):
    _, response = await _read_unrecorded(api_client, described=["booking", "reschedule"])
    assert response.status_code == 404


@pytest.mark.parametrize("kinds", [["booking"], ["booking", "reschedule"]])
async def test_temporal_down_on_read_is_a_503_not_a_404(api_client, no_row, kinds):
    """We cannot tell whether the booking exists; a 404 would say it does not -- whether
    Temporal fails on the booking or on the reschedule asked after it."""
    unavailable = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    _, response = await _read_unrecorded(
        api_client, described=kinds, **{kinds[-1]: unavailable}
    )
    assert response.status_code == 503
