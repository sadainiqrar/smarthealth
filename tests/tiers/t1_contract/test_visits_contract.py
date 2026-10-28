"""The visit endpoints' HTTP contract, with Temporal and the database faked.

The precheck (`service.require_visit_action`) is monkeypatched rather than run against a
session: what is under test here is the router's role gates, its status codes and its
error mapping, not the state table (T0) or the SQL (T3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.api.deps import get_session, get_temporal
from app.core.errors import Conflict, NotFound
from app.core.temporal import TASK_QUEUE
from app.main import app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.modules.scheduling import router as scheduling_router
from app.modules.scheduling import service
from app.modules.scheduling.models import AppointmentStatus, VisitStatus
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()
CALLER = "11111111-1111-1111-1111-111111111111"
APPOINTMENT = uuid.uuid4()

#: action path -> (workflow signal, the roles allowed, a role that is not)
ACTIONS = {
    "check-in": ("check_in", (UserRole.FRONT_DESK, UserRole.ADMIN), UserRole.PROVIDER),
    "seen": ("seen", (UserRole.PROVIDER, UserRole.ADMIN), UserRole.FRONT_DESK),
    "complete": ("complete", (UserRole.PROVIDER, UserRole.ADMIN), UserRole.PATIENT),
}


def auth(role: UserRole, subject: str = CALLER) -> dict[str, str]:
    token = create_access_token(
        subject=subject, role=role, settings=SETTINGS, now=datetime.now(UTC)
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


class _Session:
    """Nothing in these tests may reach a real query."""


def _install(temporal: FakeTemporal) -> None:
    async def _session():
        yield _Session()

    app.dependency_overrides[get_temporal] = lambda: temporal
    app.dependency_overrides[get_session] = _session


def _uninstall() -> None:
    app.dependency_overrides.pop(get_temporal, None)
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def temporal():
    fake = FakeTemporal()
    _install(fake)
    yield fake
    _uninstall()


@pytest.fixture
def precheck_passes(monkeypatch):
    calls: list = []

    async def fake(session, *, appointment_id, action):
        calls.append((appointment_id, action))

    monkeypatch.setattr(service, "require_visit_action", fake)
    return calls


@pytest.mark.parametrize("path", ACTIONS)
async def test_an_action_requires_a_token(api_client, temporal, path):
    response = await api_client.post(f"/appointments/{APPOINTMENT}/{path}")
    assert response.status_code == 401


@pytest.mark.parametrize("path", ACTIONS)
async def test_the_wrong_role_is_refused_before_anything_is_checked(
    api_client, temporal, precheck_passes, path
):
    _, _, wrong = ACTIONS[path]
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/{path}", headers=auth(wrong)
    )
    assert response.status_code == 403
    assert precheck_passes == [] and temporal.starts == []


@pytest.mark.parametrize(
    ("path", "role"), [(p, r) for p, (_, roles, _) in ACTIONS.items() for r in roles]
)
async def test_an_allowed_role_gets_202_and_signals_the_visit(
    api_client, temporal, precheck_passes, path, role
):
    """202, not 200: the write happens in the workflow, after the response."""
    signal, _, _ = ACTIONS[path]
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/{path}", headers=auth(role)
    )
    assert response.status_code == 202
    assert response.json() == {"appointment_id": str(APPOINTMENT), "action": signal}
    (start,) = temporal.starts
    assert start["id"] == f"visit-{APPOINTMENT}"
    assert start["start_signal"] == signal
    assert start["start_signal_args"][0].actor_user_id == CALLER
    assert start["task_queue"] == SETTINGS.task_queue(TASK_QUEUE)


async def test_an_illegal_action_is_a_409_and_signals_nothing(
    api_client, temporal, monkeypatch
):
    async def refuse(session, *, appointment_id, action):
        raise Conflict("cannot seen: appointment is confirmed, visit is none")

    monkeypatch.setattr(service, "require_visit_action", refuse)
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/seen", headers=auth(UserRole.PROVIDER)
    )
    assert response.status_code == 409
    assert response.json()["error"] == "Conflict"
    assert temporal.starts == []


async def test_an_unknown_appointment_is_a_404(api_client, temporal, monkeypatch):
    async def missing(session, *, appointment_id, action):
        raise NotFound(f"appointment {appointment_id} does not exist")

    monkeypatch.setattr(service, "require_visit_action", missing)
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/check-in", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404
    assert temporal.starts == []


async def test_a_finished_visit_is_a_409(api_client, precheck_passes):
    """signal-with-start on a closed id: the visit finished between precheck and signal."""
    _install(FakeTemporal(WorkflowAlreadyStartedError("visit", "Visit", run_id="r")))
    try:
        response = await api_client.post(
            f"/appointments/{APPOINTMENT}/complete", headers=auth(UserRole.PROVIDER)
        )
    finally:
        _uninstall()
    assert response.status_code == 409


async def test_temporal_down_is_a_503(api_client, precheck_passes):
    _install(FakeTemporal(RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")))
    try:
        response = await api_client.post(
            f"/appointments/{APPOINTMENT}/check-in", headers=auth(UserRole.ADMIN)
        )
    finally:
        _uninstall()
    assert response.status_code == 503
    assert response.json()["error"] == "ServiceUnavailable"


# --- GET /appointments/{id}/visit -----------------------------------------------------

PATIENT = uuid.uuid4()
NOW = datetime.now(UTC)


@pytest.fixture
def records(monkeypatch, temporal):
    """An appointment owned by PATIENT, with a checked-in visit."""
    appointment = SimpleNamespace(
        id=APPOINTMENT, patient_id=PATIENT, status=AppointmentStatus.CONFIRMED
    )
    visit = SimpleNamespace(
        appointment_id=APPOINTMENT, status=VisitStatus.CHECKED_IN,
        checked_in_at=NOW, seen_at=None, completed_at=None,
    )
    state = {"appointment": appointment, "visit": visit, "owner": PATIENT}

    async def get_appointment(session, appointment_id, **kwargs):
        return state["appointment"]

    async def get_visit(session, appointment_id, **kwargs):
        return state["visit"]

    async def own_patient_id(session, claims):
        return state["owner"]

    monkeypatch.setattr(service, "get_appointment", get_appointment)
    monkeypatch.setattr(service, "get_visit", get_visit)
    monkeypatch.setattr(scheduling_router, "_own_patient_id", own_patient_id)
    return state


async def test_reading_a_visit_requires_a_token(api_client):
    assert (await api_client.get(f"/appointments/{APPOINTMENT}/visit")).status_code == 401


@pytest.mark.parametrize("role", [UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN])
async def test_staff_read_the_visit(api_client, records, role):
    response = await api_client.get(f"/appointments/{APPOINTMENT}/visit", headers=auth(role))
    assert response.status_code == 200
    body = response.json()
    assert body["appointment_id"] == str(APPOINTMENT)
    assert body["status"] == "checked_in"
    assert body["seen_at"] is None and body["completed_at"] is None


async def test_the_owning_patient_reads_the_visit(api_client, records):
    response = await api_client.get(
        f"/appointments/{APPOINTMENT}/visit", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 200


async def test_another_patients_visit_is_a_404_not_a_403(api_client, records):
    records["owner"] = uuid.uuid4()
    response = await api_client.get(
        f"/appointments/{APPOINTMENT}/visit", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 404


async def test_no_visit_yet_is_a_404(api_client, records):
    records["visit"] = None
    response = await api_client.get(
        f"/appointments/{APPOINTMENT}/visit", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404


async def test_an_unknown_appointment_has_no_visit(api_client, records):
    records["appointment"] = None
    response = await api_client.get(
        f"/appointments/{APPOINTMENT}/visit", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404
