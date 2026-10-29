"""The visit endpoints' HTTP contract, with Temporal, the database and the audit log
faked.

The service functions are monkeypatched rather than run against a session: what is under
test here is the router -- role gates, status codes, error mapping, that it commits what
the service wrote, and that the post-commit check-in signal can never fail a request.
The state table is T0's; the SQL and its locking are T3's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.service import RPCError, RPCStatusCode

from app.api.deps import get_audit_log, get_session, get_temporal
from app.core.errors import Conflict, NotFound
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
NOW = datetime.now(UTC)

#: action path -> (service function, resulting visit status, allowed roles, a wrong role)
ACTIONS = {
    "check-in": (
        "check_in", VisitStatus.CHECKED_IN, (UserRole.FRONT_DESK, UserRole.ADMIN),
        UserRole.PROVIDER,
    ),
    "seen": (
        "mark_seen", VisitStatus.IN_PROGRESS, (UserRole.PROVIDER, UserRole.ADMIN),
        UserRole.FRONT_DESK,
    ),
    "complete": (
        "complete_visit", VisitStatus.COMPLETED, (UserRole.PROVIDER, UserRole.ADMIN),
        UserRole.PATIENT,
    ),
}


def auth(role: UserRole, subject: str = CALLER) -> dict[str, str]:
    token = create_access_token(
        subject=subject, role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


class FakeHandle:
    def __init__(self, temporal: FakeTemporal, workflow_id: str) -> None:
        self._temporal = temporal
        self._id = workflow_id

    async def signal(self, signal, *args) -> None:
        if self._temporal.error:
            raise self._temporal.error
        name = getattr(signal, "__name__", signal)
        self._temporal.signals.append((self._id, name))


class FakeTemporal:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.session = FakeSession()

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


class FakeSession:
    """Records commits; nothing may reach a real query."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class FakeAudit:
    async def record(self, event) -> None:  # pragma: no cover - the service is faked
        raise AssertionError("the router must not audit; the service does")


@pytest.fixture
def temporal():
    fake = FakeTemporal()

    async def _session():
        yield fake.session

    app.dependency_overrides[get_temporal] = lambda: fake
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_audit_log] = FakeAudit
    yield fake
    for dependency in (get_temporal, get_session, get_audit_log):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def writes(monkeypatch):
    """The three service writes, faked: each records its call and returns a visit in the
    state that action produces. `fail` makes every call raise instead."""
    state: dict = {"calls": [], "fail": None}

    def make(name: str, status: VisitStatus):
        async def fake(session, audit, *, appointment_id, now, actor_user_id):
            state["calls"].append((name, appointment_id, actor_user_id))
            if state["fail"]:
                raise state["fail"]
            return SimpleNamespace(
                appointment_id=appointment_id, status=status, checked_in_at=NOW,
                seen_at=NOW if status is not VisitStatus.CHECKED_IN else None,
                completed_at=NOW if status is VisitStatus.COMPLETED else None,
            )

        return fake

    for name, status, _, _ in ACTIONS.values():
        monkeypatch.setattr(service, name, make(name, status))
    return state


@pytest.mark.parametrize("path", ACTIONS)
async def test_an_action_requires_a_token(api_client, temporal, path):
    response = await api_client.post(f"/appointments/{APPOINTMENT}/{path}")
    assert response.status_code == 401


@pytest.mark.parametrize("path", ACTIONS)
async def test_the_wrong_role_is_refused_before_anything_is_written(
    api_client, temporal, writes, path
):
    *_, wrong = ACTIONS[path]
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/{path}", headers=auth(wrong)
    )
    assert response.status_code == 403
    assert writes["calls"] == [] and temporal.session.commits == 0
    assert temporal.signals == []


@pytest.mark.parametrize(
    ("path", "role"), [(p, r) for p, (_, _, roles, _) in ACTIONS.items() for r in roles]
)
async def test_an_allowed_role_gets_200_with_the_written_visit(
    api_client, temporal, writes, path, role
):
    """200, not 202: the write is done, in this request's transaction, by the time the
    response leaves -- and the actor is the caller from the token."""
    name, status, _, _ = ACTIONS[path]
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/{path}", headers=auth(role)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["appointment_id"] == str(APPOINTMENT)
    assert body["status"] == status.value
    assert writes["calls"] == [(name, APPOINTMENT, CALLER)]
    assert temporal.session.commits == 1


async def test_only_check_in_signals_the_visit_workflow(api_client, temporal, writes):
    await api_client.post(
        f"/appointments/{APPOINTMENT}/check-in", headers=auth(UserRole.FRONT_DESK)
    )
    assert temporal.signals == [(f"visit-{APPOINTMENT}", "checked_in")]
    for path in ("seen", "complete"):
        await api_client.post(
            f"/appointments/{APPOINTMENT}/{path}", headers=auth(UserRole.PROVIDER)
        )
    assert len(temporal.signals) == 1


@pytest.mark.parametrize("path", ACTIONS)
async def test_an_illegal_action_is_a_409_and_commits_and_signals_nothing(
    api_client, temporal, writes, path
):
    """A repeat included: the service is strict under its lock, so a second check-in is
    refused, not silently accepted."""
    writes["fail"] = Conflict("cannot check_in: appointment is confirmed, visit is checked_in")
    _, _, roles, _ = ACTIONS[path]
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/{path}", headers=auth(roles[0])
    )
    assert response.status_code == 409
    assert response.json()["error"] == "Conflict"
    assert temporal.session.commits == 0 and temporal.signals == []


async def test_an_unknown_appointment_is_a_404(api_client, temporal, writes):
    writes["fail"] = NotFound(f"appointment {APPOINTMENT} does not exist")
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/check-in", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404
    assert temporal.session.commits == 0 and temporal.signals == []


@pytest.mark.parametrize(
    "error",
    [
        RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""),
        RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""),
        RuntimeError("Failed client connect"),
    ],
    ids=["unavailable", "not-found", "cannot-connect"],
)
async def test_temporal_failing_after_the_commit_still_answers_200(
    api_client, temporal, writes, error
):
    """The check-in is committed before the signal is sent; the no-show timer reconciles
    against the database if the signal is lost. A 503 here would tell staff a recorded
    check-in failed -- and their retry would then be a 409."""
    temporal.error = error
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/check-in", headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "checked_in"
    assert temporal.session.commits == 1


# --- GET /appointments/{id}/visit -----------------------------------------------------

PATIENT = uuid.uuid4()


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
