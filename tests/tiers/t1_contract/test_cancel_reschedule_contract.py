"""Cancel, reschedule and waitlist: the HTTP contract, with Temporal, the database and the
audit log faked.

Service functions are monkeypatched rather than run against a session: what is under test
is the router -- role gates, ownership, status codes, error mapping, that cancel commits
what the service wrote and then can never be failed by Temporal, and that a reschedule
that cannot start is a 503. The SQL, the locking and the real workflows are T3's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.api.deps import get_audit_log, get_session, get_temporal
from app.core.errors import Conflict, NotFound
from app.main import app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.modules.scheduling import router as scheduling_router
from app.modules.scheduling import service
from app.modules.scheduling.booking import (
    appointment_id_for,
    reschedule_appointment_id_for,
)
from app.modules.scheduling.models import AppointmentStatus, WaitlistStatus
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()
CALLER = "11111111-1111-1111-1111-111111111111"
APPOINTMENT = uuid.uuid4()
PATIENT = uuid.uuid4()
SLOT = uuid.uuid4()
NEW_SLOT = uuid.uuid4()
NOW = datetime.now(UTC)
STAFF = (UserRole.FRONT_DESK, UserRole.ADMIN)
CANCEL_BODY = {"reason": "cannot make it"}
RESCHEDULE_BODY = {"new_slot_id": str(NEW_SLOT), "reason": "clash"}


def auth(role: UserRole, subject: str = CALLER) -> dict[str, str]:
    token = create_access_token(
        subject=subject, role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


class _Description:
    def __init__(self, memo: dict) -> None:
        self._memo = memo

    async def memo_value(self, key, default=None):
        return self._memo.get(key, default)


class _Handle:
    def __init__(self, memo: dict) -> None:
        self._memo = memo

    async def describe(self):
        return _Description(self._memo)


class FakeTemporal:
    """Records starts; like Temporal, keeps the memo of the first start under an id."""

    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.requests: list[object] = []
        self.error: Exception | None = None
        self._memos: dict[str, dict] = {}
        self.session = FakeSession()

    async def start_workflow(self, run, request, **kwargs):
        if self.error:
            raise self.error
        self.starts.append(kwargs)
        self.requests.append(request)
        self._memos.setdefault(kwargs["id"], kwargs.get("memo") or {})

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        return _Handle(self._memos.get(workflow_id, {}))


class FakeSession:
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


def _appointment(status: AppointmentStatus, **overrides) -> SimpleNamespace:
    values = {
        "id": APPOINTMENT, "status": status, "patient_id": PATIENT, "provider_id": uuid.uuid4(),
        "clinic_id": uuid.uuid4(), "slot_id": SLOT, "reason": None, "confirmed_at": NOW,
        "created_at": NOW, "rescheduled_from_id": None,
    }
    return SimpleNamespace(**{**values, **overrides})


@pytest.fixture
def world(monkeypatch, temporal):
    """One appointment owned by PATIENT, `confirmed` with no visit. The service writes are
    faked: cancel records its call and returns the appointment as cancelled."""
    state: dict = {
        "appointment": _appointment(AppointmentStatus.CONFIRMED), "visit": None,
        "owner": PATIENT, "new_row": None, "cancel_error": None, "cancels": [],
        "waitlist_calls": [], "waitlist_error": None,
    }

    async def get_appointment(session, appointment_id, **kwargs):
        if appointment_id == APPOINTMENT:
            return state["appointment"]
        return state["new_row"]

    async def get_visit(session, appointment_id, **kwargs):
        return state["visit"]

    async def cancel_appointment(session, audit, **kwargs):
        state["cancels"].append(kwargs)
        if state["cancel_error"]:
            raise state["cancel_error"]
        return _appointment(AppointmentStatus.CANCELLED)

    async def own_patient_id(session, claims):
        return state["owner"]

    async def add_waitlist_entry(session, audit, **kwargs):
        state["waitlist_calls"].append(("add", kwargs))
        if state["waitlist_error"]:
            raise state["waitlist_error"]
        return SimpleNamespace(
            id=uuid.uuid4(), status=WaitlistStatus.ACTIVE, created_at=NOW,
            **{k: kwargs[k] for k in (
                "patient_id", "provider_id", "department_id", "window_start", "window_end"
            )},
        )

    async def list_waitlist(session, **kwargs):
        state["waitlist_calls"].append(("list", kwargs))
        return [], 0

    monkeypatch.setattr(service, "get_appointment", get_appointment)
    monkeypatch.setattr(service, "get_visit", get_visit)
    monkeypatch.setattr(service, "cancel_appointment", cancel_appointment)
    monkeypatch.setattr(service, "add_waitlist_entry", add_waitlist_entry)
    monkeypatch.setattr(service, "list_waitlist", list_waitlist)
    monkeypatch.setattr(scheduling_router, "_own_patient_id", own_patient_id)
    return state


# --- cancel ---------------------------------------------------------------------------


async def test_cancel_requires_a_token(api_client, temporal):
    response = await api_client.post(f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY)
    assert response.status_code == 401


async def test_a_provider_may_not_cancel(api_client, temporal, world):
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(UserRole.PROVIDER)
    )
    assert response.status_code == 403
    assert world["cancels"] == [] and temporal.session.commits == 0 and temporal.starts == []


@pytest.mark.parametrize("role", [UserRole.PATIENT, *STAFF])
async def test_cancel_answers_200_commits_and_starts_the_follow_up(
    api_client, temporal, world, role
):
    """200, not 202: the cancellation is done, in this request's transaction, by the time
    the response leaves -- and the follow-up is told which slot it freed."""
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(role)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["id"] == str(APPOINTMENT)
    assert world["cancels"][0]["reason"] == "cannot make it"
    assert world["cancels"][0]["actor_user_id"] == CALLER
    assert temporal.session.commits == 1
    assert temporal.starts[0]["id"] == f"cancel-{APPOINTMENT}"
    follow_up = temporal.requests[0]
    assert follow_up.appointment_id == str(APPOINTMENT)
    assert follow_up.slot_id == str(SLOT)


async def test_a_patient_cannot_cancel_someone_elses_appointment(api_client, temporal, world):
    """404, not 403: a patient must not be able to probe which appointment ids exist."""
    world["owner"] = uuid.uuid4()
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 404
    assert world["cancels"] == [] and temporal.session.commits == 0 and temporal.starts == []


async def test_cancelling_an_unknown_appointment_is_a_404(api_client, temporal, world):
    world["appointment"] = None
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 404
    assert temporal.starts == []


async def test_an_illegal_cancel_is_a_409_and_commits_and_starts_nothing(
    api_client, temporal, world
):
    world["cancel_error"] = Conflict("an appointment that is cancelled cannot be cancelled")
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 409
    assert temporal.session.commits == 0 and temporal.starts == []


@pytest.mark.parametrize(
    "error",
    [
        RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""),
        RuntimeError("Failed client connect"),
        WorkflowAlreadyStartedError("cancel-x", "CancelFollowUp"),
    ],
    ids=["unavailable", "cannot-connect", "already-started"],
)
async def test_temporal_failing_after_the_commit_still_answers_200(
    api_client, temporal, world, error
):
    """The cancellation is committed before the follow-up starts. A 503 would tell the
    caller a recorded cancellation failed, and their retry would be a 409."""
    temporal.error = error
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=CANCEL_BODY, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert temporal.session.commits == 1


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "x" * 2001}])
async def test_cancel_needs_a_reason(api_client, temporal, world, body):
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/cancel", json=body, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422
    assert world["cancels"] == []


# --- reschedule -----------------------------------------------------------------------


async def test_reschedule_requires_a_token(api_client, temporal):
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY
    )
    assert response.status_code == 401


async def test_a_provider_may_not_reschedule(api_client, temporal, world):
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.PROVIDER),
    )
    assert response.status_code == 403
    assert temporal.starts == []


@pytest.mark.parametrize("role", [UserRole.PATIENT, *STAFF])
async def test_reschedule_is_accepted_not_done(api_client, temporal, world, role):
    """202: the new appointment is pending, and the old one stands until it confirms."""
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=auth(role)
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["rescheduled_from"] == str(APPOINTMENT)
    assert body["appointment_id"] != str(APPOINTMENT)
    assert temporal.starts[0]["id"] == f"reschedule-{body['appointment_id']}"
    request = temporal.requests[0]
    assert request.old_appointment_id == str(APPOINTMENT)
    assert request.new_appointment_id == body["appointment_id"]
    assert request.patient_id == str(PATIENT), "the new booking is for the old one's patient"
    assert request.new_slot_id == str(NEW_SLOT)
    assert request.actor_user_id == CALLER and request.reason == "clash"
    assert temporal.session.commits == 0, "the request itself writes nothing"


async def test_a_patient_cannot_reschedule_someone_elses_appointment(
    api_client, temporal, world
):
    world["owner"] = uuid.uuid4()
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.PATIENT),
    )
    assert response.status_code == 404
    assert temporal.starts == []


async def test_rescheduling_an_unknown_appointment_is_a_404(api_client, temporal, world):
    world["appointment"] = None
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 404
    assert temporal.starts == []


@pytest.mark.parametrize(
    "status",
    [
        AppointmentStatus.PENDING, AppointmentStatus.CANCELLED, AppointmentStatus.RESCHEDULED,
        AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.FAILED,
    ],
)
async def test_only_a_confirmed_appointment_can_be_rescheduled(
    api_client, temporal, world, status
):
    world["appointment"] = _appointment(status)
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 409
    assert temporal.starts == []


async def test_an_appointment_whose_own_reschedule_is_completing_is_a_409(
    api_client, temporal, world
):
    """Its predecessor is still `confirmed`, so `complete_reschedule` has not run: a second
    reschedule now could leave both confirmed."""
    predecessor = uuid.uuid4()
    world["appointment"] = _appointment(
        AppointmentStatus.CONFIRMED, rescheduled_from_id=predecessor
    )
    world["new_row"] = _appointment(AppointmentStatus.CONFIRMED, id=predecessor)
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 409
    assert "still completing" in response.text
    assert temporal.starts == []


async def test_a_checked_in_patient_cannot_be_rescheduled(api_client, temporal, world):
    world["visit"] = SimpleNamespace(appointment_id=APPOINTMENT)
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 409
    assert temporal.starts == []


@pytest.mark.parametrize(
    "error",
    [
        RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""),
        RuntimeError("Failed client connect"),
    ],
    ids=["unavailable", "cannot-connect"],
)
async def test_a_reschedule_that_cannot_start_is_a_503(api_client, temporal, world, error):
    """Nothing was written, so unlike cancel the caller must be told to retry."""
    temporal.error = error
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 503


async def test_the_same_idempotency_key_names_the_same_new_appointment(
    api_client, temporal, world
):
    headers = {**auth(UserRole.ADMIN), "Idempotency-Key": "abc"}
    first = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=headers
    )
    second = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=headers
    )
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    # In the reschedule namespace: the same key on a booking names another appointment.
    new_id = first.json()["appointment_id"]
    assert new_id == str(reschedule_appointment_id_for(CALLER, "abc"))
    assert new_id != str(appointment_id_for(CALLER, "abc"))


async def test_a_repeat_after_the_reschedule_completed_is_still_202(
    api_client, temporal, world
):
    """Once the first reschedule finishes, the old appointment is `rescheduled` -- which
    the precheck would refuse with a 409. A repeat of the accepted request (same key) must
    get the answer the first one did, so the precheck steps aside for it."""
    headers = {**auth(UserRole.ADMIN), "Idempotency-Key": "abc"}
    first = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=headers
    )
    new_id = uuid.UUID(first.json()["appointment_id"])
    world["appointment"] = _appointment(AppointmentStatus.RESCHEDULED)
    world["new_row"] = _appointment(
        AppointmentStatus.CONFIRMED, id=new_id, rescheduled_from_id=APPOINTMENT
    )
    again = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=headers
    )
    assert again.status_code == 202
    assert again.json() == first.json()


async def test_a_key_reused_for_a_different_reschedule_is_a_422(api_client, temporal, world):
    headers = {**auth(UserRole.ADMIN), "Idempotency-Key": "abc"}
    await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY, headers=headers
    )
    other = {**RESCHEDULE_BODY, "new_slot_id": str(uuid.uuid4())}
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=other, headers=headers
    )
    assert response.status_code == 422


async def test_an_empty_idempotency_key_is_a_422(api_client, temporal, world):
    response = await api_client.post(
        f"/appointments/{APPOINTMENT}/reschedule", json=RESCHEDULE_BODY,
        headers={**auth(UserRole.ADMIN), "Idempotency-Key": ""},
    )
    assert response.status_code == 422
    assert temporal.starts == []


# --- waitlist -------------------------------------------------------------------------

WINDOW = {
    "window_start": (NOW + timedelta(days=1)).isoformat(),
    "window_end": (NOW + timedelta(days=3)).isoformat(),
}


def _join(**overrides) -> dict:
    return {"patient_id": str(PATIENT), **WINDOW, **overrides}


async def test_joining_requires_a_token(api_client, temporal):
    assert (await api_client.post("/waitlist", json=_join())).status_code == 401


async def test_a_provider_may_not_join_the_waitlist(api_client, temporal, world):
    response = await api_client.post(
        "/waitlist", json=_join(), headers=auth(UserRole.PROVIDER)
    )
    assert response.status_code == 403
    assert world["waitlist_calls"] == []


@pytest.mark.parametrize("role", [UserRole.PATIENT, *STAFF])
async def test_joining_answers_201_with_the_entry(api_client, temporal, world, role):
    response = await api_client.post("/waitlist", json=_join(), headers=auth(role))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["patient_id"] == str(PATIENT) and body["status"] == "active"
    assert world["waitlist_calls"][0][1]["actor_user_id"] == CALLER
    assert temporal.session.commits == 1


async def test_a_patient_may_only_join_for_themselves(api_client, temporal, world):
    response = await api_client.post(
        "/waitlist", json=_join(patient_id=str(uuid.uuid4())), headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 403
    assert world["waitlist_calls"] == [] and temporal.session.commits == 0


async def test_staff_may_join_for_anyone(api_client, temporal, world):
    other = uuid.uuid4()
    response = await api_client.post(
        "/waitlist", json=_join(patient_id=str(other)), headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 201
    assert world["waitlist_calls"][0][1]["patient_id"] == other


@pytest.mark.parametrize("field", ["window_start", "window_end"])
async def test_a_window_bound_without_an_offset_is_a_422(api_client, temporal, world, field):
    """Ambiguous, and comparing it with an aware slot time would raise -- a 500."""
    naive = (datetime.now() + timedelta(days=2)).replace(tzinfo=None).isoformat()
    response = await api_client.post(
        "/waitlist", json=_join(**{field: naive}), headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422
    assert world["waitlist_calls"] == []


@pytest.mark.parametrize("days", [0, -1], ids=["empty", "backwards"])
async def test_a_window_that_does_not_move_forward_is_a_422(api_client, temporal, world, days):
    end = (NOW + timedelta(days=1) + timedelta(days=days)).isoformat()
    response = await api_client.post(
        "/waitlist", json=_join(window_end=end), headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422
    assert world["waitlist_calls"] == []


async def test_an_unknown_patient_is_a_404(api_client, temporal, world):
    world["waitlist_error"] = NotFound("patient does not exist")
    response = await api_client.post(
        "/waitlist", json=_join(), headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 404
    assert temporal.session.commits == 0


async def test_listing_requires_a_token(api_client, temporal):
    assert (await api_client.get("/waitlist")).status_code == 401


async def test_a_provider_may_not_list_the_waitlist(api_client, temporal, world):
    """Spec 4.5: the list is for staff. It names every waiting patient, for any provider."""
    response = await api_client.get("/waitlist", headers=auth(UserRole.PROVIDER))
    assert response.status_code == 403
    assert world["waitlist_calls"] == []


@pytest.mark.parametrize("role", STAFF)
async def test_staff_list_with_an_optional_patient_filter(api_client, temporal, world, role):
    other = uuid.uuid4()
    response = await api_client.get(f"/waitlist?patient_id={other}", headers=auth(role))
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert world["waitlist_calls"][0][1]["patient_id"] == other
    await api_client.get("/waitlist", headers=auth(role))
    assert world["waitlist_calls"][1][1]["patient_id"] is None


async def test_a_patients_list_is_forced_to_their_own_entries(api_client, temporal, world):
    """Whatever `patient_id` they send, a patient sees their own."""
    response = await api_client.get(
        f"/waitlist?patient_id={uuid.uuid4()}", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 200
    assert world["waitlist_calls"][0][1]["patient_id"] == PATIENT


async def test_a_patient_with_no_patient_record_sees_nothing_not_everything(
    api_client, temporal, world
):
    """`patient_id=None` means "everyone" to the service; a login with no patient record
    must never reach it."""
    world["owner"] = None
    response = await api_client.get("/waitlist", headers=auth(UserRole.PATIENT))
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["total"] == 0
    assert world["waitlist_calls"] == []
