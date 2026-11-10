"""The assistant agent over gRPC: guided flows, confirmations, chat, degradation (spec 9).

The real servicer and LangGraph graph, an in-memory checkpointer, a fake Part A
(tests/harness/fake_part_a.py) and a fake knowledge service with canned passages. No model
unless a test brings a scripted one: with `model=None` typed messages take the offline path,
which is what compose runs until there is a key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

import tests.harness.partb  # noqa: F401 - the services' import paths
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token, generate_signing_key, public_jwk
from app.settings import Settings
from tests.harness.fake_part_a import FakePartA

from assistant_service.agent import Agent  # noqa: E402
from assistant_service.auth import TokenVerifier  # noqa: E402
from assistant_service.llm import LLMUnavailable  # noqa: E402
from assistant_service.parta import Breaker, PartA  # noqa: E402
from assistant_service.server import build_server  # noqa: E402
from assistant_service.stores import InMemoryStores  # noqa: E402
from smarthealth.assistant.v1 import assistant_pb2, assistant_pb2_grpc  # noqa: E402
from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc  # noqa: E402

pytestmark = pytest.mark.contract

PART_A = Settings(jwt_private_key=generate_signing_key())
PASSAGES = [
    knowledge_pb2.Hit(
        source_id="doc:prep-blood-tests#fasting-blood-test",
        kind="document",
        title="Fasting blood test",
        snippet="Do not eat or drink anything except water for 8 to 12 hours.",
        score=0.9,
    ),
    knowledge_pb2.Hit(
        source_id="doc:policies#late-cancellation",
        kind="document",
        title="Late cancellation",
        snippet="Cancelling less than 24 hours before costs a fee of 20.",
        score=0.8,
    ),
]


def _jwks(_url):
    return {"keys": [public_jwk(PART_A)]}


def _token(role=UserRole.PATIENT, *, user="u-patient") -> str:
    return create_access_token(
        subject=user, role=role, session_id="s1", settings=PART_A, now=datetime.now(UTC)
    )


def _auth(token):
    return (("authorization", f"Bearer {token}"),)


class FakeKnowledge(knowledge_pb2_grpc.KnowledgeServicer):
    def __init__(self) -> None:
        self.queries: list[tuple[str, str]] = []

    async def Search(self, request, context):  # noqa: N802
        self.queries.append((request.query, dict(context.invocation_metadata())["authorization"]))
        words = set(request.query.lower().split())
        hits = [h for h in PASSAGES if words & set((h.title + " " + h.snippet).lower().split())]
        return knowledge_pb2.SearchReply(hits=hits)


class Scripted(BaseChatModel):
    """A model test double: returns the next scripted message, whatever it is asked."""

    script: list[Any]
    calls: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **_):  # noqa: ANN001
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])


class Harness:
    def __init__(
        self, fake: FakePartA, model: BaseChatModel | None = None, knowledge_up: bool = True
    ) -> None:
        self.fake, self.model, self.knowledge_up = fake, model, knowledge_up
        self.stores = InMemoryStores()
        self.knowledge = FakeKnowledge()

    async def __aenter__(self) -> Harness:
        self._ks = grpc.aio.server()
        knowledge_pb2_grpc.add_KnowledgeServicer_to_server(self.knowledge, self._ks)
        port = self._ks.add_insecure_port("[::]:0")
        if self.knowledge_up:
            await self._ks.start()
        self._kc = grpc.aio.insecure_channel(f"localhost:{port if self.knowledge_up else 1}")
        self.parta = PartA(
            "http://part-a",
            transport=self.fake.transport(),
            breaker=Breaker(failure_threshold=2, reset_seconds=60),
        )
        self._server = build_server(
            self.stores,
            TokenVerifier("jwks", PART_A.jwt_issuer, fetch=_jwks),
            self._kc,
            0,
            agent=Agent(MemorySaver()),
            parta=self.parta,
            model=self.model,
            model_label=("groq", "test-model"),
            prices=(0.15, 0.60),
            token_delay=0,
        )
        await self._server.start()
        self._channel = grpc.aio.insecure_channel(f"localhost:{self._server.port}")
        self.stub = assistant_pb2_grpc.AssistantStub(self._channel)
        self._turn = 0
        return self

    async def __aexit__(self, *exc) -> None:
        await self._channel.close()
        await self._server.stop(None)
        await self._kc.close()
        await self._ks.stop(None)
        await self.parta.aclose()

    def next_turn(self) -> str:
        self._turn += 1
        return f"turn-{self._turn}"

    async def chat(
        self,
        token: str,
        *,
        conversation="c1",
        message="",
        starter="",
        choice: tuple[str, str] | None = None,
        turn: str | None = None,
        resume=0,
    ):
        request = assistant_pb2.ChatRequest(
            conversation_id=conversation,
            turn_id=turn or self.next_turn(),
            message=message,
            starter_id=starter,
            resume_from=resume,
        )
        if choice:
            request.choice.CopyFrom(
                assistant_pb2.OptionChoice(options_id=choice[0], option_id=choice[1])
            )
        return [e async for e in self.stub.Chat(request, metadata=_auth(token))]

    async def confirm(
        self,
        token: str,
        confirmation_id: str,
        approved: bool,
        *,
        conversation="c1",
        turn: str | None = None,
    ):
        request = assistant_pb2.ConfirmRequest(
            conversation_id=conversation,
            turn_id=turn or self.next_turn(),
            confirmation_id=confirmation_id,
            approved=approved,
        )
        return [e async for e in self.stub.Confirm(request, metadata=_auth(token))]


def kinds(events) -> list[str]:
    return [e.WhichOneof("kind") for e in events]


def text(events) -> str:
    return "".join(e.token for e in events if e.WhichOneof("kind") == "token")


def last(events, kind: str):
    return [getattr(e, kind) for e in events if e.WhichOneof("kind") == kind][-1]


def option(options, label_prefix: str) -> str:
    return next(o.id for o in options.options if o.label.startswith(label_prefix))


async def _book_until_confirmation(h: Harness, token: str, conversation="c1"):
    events = await h.chat(token, conversation=conversation, starter="book")
    specialties = last(events, "options")
    assert events[-1].done.reason == "awaiting_input"
    assert specialties.style == "chips"
    events = await h.chat(
        token, conversation=conversation, choice=(specialties.options_id, "Cardiology")
    )
    doctors = last(events, "options")
    assert doctors.options[0].label == "First available"
    events = await h.chat(token, conversation=conversation, choice=(doctors.options_id, "any"))
    times = last(events, "options")
    assert times.options[0].badge == "Earliest"
    events = await h.chat(
        token, conversation=conversation, choice=(times.options_id, times.options[0].id)
    )
    return last(events, "confirmation"), times.options[0].id


async def test_a_patient_books_by_clicking_and_nothing_happens_before_the_yes():
    """Referenced by tests/cases/pb-005-assistant-books-only-after-confirmation.yaml."""
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token()
        confirmation, slot_id = await _book_until_confirmation(h, token)
        assert confirmation.action == "book_appointment"
        assert {d.label for d in confirmation.details} >= {"Patient", "Doctor", "When"}
        assert fake.bookings == []  # asked, not done
        events = await h.confirm(token, confirmation.confirmation_id, True)
    card = last(events, "card")
    assert card.title == "Appointment confirmed" and card.link.startswith("/bookings/")
    [(_, _, body, headers)] = fake.bookings
    assert body["slot_id"] == slot_id and body["patient_id"] == fake.patient["id"]
    assert headers["idempotency-key"].startswith("assistant-")
    assert headers["x-conversation-ref"] == "c1"
    assert headers["authorization"] == f"Bearer {token}"  # the user's own token
    assert events[-1].done.reason == "complete"


async def test_a_declined_confirmation_writes_nothing():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        confirmation, _ = await _book_until_confirmation(h, _token())
        events = await h.confirm(_token(), confirmation.confirmation_id, False)
    assert fake.bookings == []
    assert "haven't booked" in text(events)


async def test_a_replayed_confirm_returns_the_same_turn_and_books_once():
    """A retry of the same Confirm turn replays its record; a second "yes" on an answered
    confirmation is refused as stale -- either way one booking."""
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token()
        confirmation, _ = await _book_until_confirmation(h, token)
        first = await h.confirm(token, confirmation.confirmation_id, True, turn="confirm-1")
        again = await h.confirm(token, confirmation.confirmation_id, True, turn="confirm-1")
        stale = await h.confirm(token, confirmation.confirmation_id, True, turn="confirm-2")
    assert [e.seq for e in again] == [e.seq for e in first]
    assert len(fake.bookings) == 1
    assert last(stale, "error").code == "stale"


async def test_a_slot_taken_meanwhile_is_said_and_new_times_offered():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token()
        confirmation, slot_id = await _book_until_confirmation(h, token)
        next(s for s in fake.slots if s["id"] == slot_id)["status"] = "booked"  # someone else
        events = await h.confirm(token, confirmation.confirmation_id, True)
    assert "no longer free" in text(events)
    assert slot_id not in [o.id for o in last(events, "options").options]
    assert events[-1].done.reason == "awaiting_input"


async def test_an_answer_to_an_old_question_is_refused():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        events = await h.chat(_token(), starter="book")
        events = await h.chat(_token(), choice=("opt-not-the-open-one", "Cardiology"))
    assert last(events, "error").code == "stale"


async def test_typed_text_mid_flow_starts_over_with_the_new_request():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        await h.chat(_token(), starter="book")
        events = await h.chat(_token(), message="how should I take my prescription?")
    card = last(events, "card")
    assert card.title == "Amoxicillin"
    assert {f.label: f.value for f in card.fields}[
        "Instructions"
    ] == "Take with food. Finish the course."
    assert "ask your doctor or pharmacist" in text(events)


async def test_front_desk_finds_the_patient_by_typing_then_clicks():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token(UserRole.FRONT_DESK, user="u-desk")
        events = await h.chat(token, starter="book")
        assert "MRN or name" in text(events) and events[-1].done.reason == "awaiting_input"
        events = await h.chat(token, message="1042")
    assert last(events, "options").style == "chips"  # patient found; on to the specialty


async def test_a_red_flag_gets_urgent_guidance_and_no_booking_even_if_asked():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        events = await h.chat(_token(), message="book me in, I have crushing chest pain")
    assert "emergency" in text(events).lower()
    assert "options" not in kinds(events) and "confirmation" not in kinds(events)
    assert last(events, "citation").source_id == "doc:urgent-care"


async def test_offline_a_question_is_answered_from_the_documents_with_sources():
    async with Harness(FakePartA.seeded()) as h:
        token = _token()
        events = await h.chat(token, message="is there a fee for late cancellation")
    assert "Late cancellation" in text(events)
    assert last(events, "citation").source_id.startswith("doc:")
    assert h.knowledge.queries[0][1] == f"Bearer {token}"  # the caller's audience, not ours
    assert last(events, "suggestions").items


async def test_offline_a_request_is_routed_to_its_flow():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        events = await h.chat(_token(), message="I need to prepare for my fasting blood test")
    assert "Fasting glucose" in text(events)
    assert "8 to 12 hours" in text(events)
    assert last(events, "citation").source_id == "doc:prep-blood-tests#fasting-blood-test"


async def test_starters_are_chosen_by_role_on_the_server():
    async with Harness(FakePartA.seeded()) as h:

        def ids(reply):
            return {s.id for g in reply.groups for s in g.starters}

        patient = ids(
            await h.stub.Starters(assistant_pb2.StartersRequest(), metadata=_auth(_token()))
        )
        provider = ids(
            await h.stub.Starters(
                assistant_pb2.StartersRequest(),
                metadata=_auth(_token(UserRole.PROVIDER, user="u-ada")),
            )
        )
    assert {"book", "cancel", "prescriptions"} <= patient
    assert not {"brief", "capacity", "snapshot"} & patient
    assert {"brief", "my_day"} <= provider and "book" not in provider


async def test_a_patient_cannot_start_a_staff_flow_by_naming_it():
    async with Harness(FakePartA.seeded()) as h:
        events = await h.chat(_token(), starter="brief")
    assert "isn't something I can help you with" in text(events)


async def test_part_a_down_is_said_plainly_and_knowledge_still_answers():
    fake = FakePartA.seeded()
    fake.down = True
    async with Harness(fake) as h:
        booking = await h.chat(_token(), starter="book")
        question = await h.chat(_token(), message="late cancellation fee", conversation="c2")
    assert "can't reach the booking system" in text(booking)
    assert last(booking, "tool").status == "failed"
    assert "Late cancellation" in text(question)


async def test_knowledge_down_still_answers_and_flows_still_work():
    fake = FakePartA.seeded()
    async with Harness(fake, knowledge_up=False) as h:
        question = await h.chat(_token(), message="late cancellation fee")
        flow = await h.chat(_token(), message="show my appointments", conversation="c2")
    assert "can't reach the clinic's guides" in text(question)
    assert question[-1].done.reason == "complete"
    assert "no upcoming appointments" in text(flow)


async def test_the_model_hands_actions_to_a_flow_and_never_writes():
    fake = FakePartA.seeded()
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "start_flow",
                        "args": {"flow_id": "book", "specialty": "cardiology"},
                        "id": "call-1",
                    }
                ],
            )
        ]
    )
    async with Harness(fake, model=model) as h:
        events = await h.chat(_token(), message="get me in with a heart doctor")
    doctors = last(events, "options")
    assert doctors.options[0].label == "First available"  # specialty prefilled, chips skipped
    assert fake.writes == []


async def test_the_model_answers_from_search_results_with_citations():
    model = Scripted(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge",
                        "args": {"query": "fasting blood test"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="Only water for 8 to 12 hours before."),
        ]
    )
    async with Harness(FakePartA.seeded(), model=model) as h:
        events = await h.chat(_token(), message="can I eat before a fasting blood test?")
    assert "8 to 12 hours" in text(events)
    assert last(events, "citation").source_id == "doc:prep-blood-tests#fasting-blood-test"
    tool_message = model.calls[1][-1]
    assert "Do not eat or drink" in tool_message.content


async def test_a_model_failure_degrades_to_the_offline_answer():
    model = Scripted(script=[LLMUnavailable("no recording")])
    async with Harness(FakePartA.seeded(), model=model) as h:
        events = await h.chat(_token(), message="is there a late cancellation fee")
    assert "Late cancellation" in text(events)
    assert events[-1].done.reason == "complete"


async def test_a_resumed_turn_replays_instead_of_running_again():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        first = await h.chat(_token(), message="late cancellation fee", turn="t-r")
        resumed = await h.chat(_token(), message="ignored", turn="t-r", resume=2)
    assert [e.seq for e in resumed] == [e.seq for e in first if e.seq > 2]
    assert len(h.knowledge.queries) == 1


async def test_one_user_cannot_answer_another_users_conversation():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        events = await h.chat(_token(), starter="book", conversation="shared")
        specialties = last(events, "options")
        intruder = await h.chat(
            _token(user="u-other"),
            conversation="shared",
            choice=(specialties.options_id, "Cardiology"),
        )
    assert last(intruder, "error").code == "stale"  # their thread has no such question


async def test_a_cancelled_turn_ends_with_done_cancelled():
    async with Harness(FakePartA.seeded()) as h:
        await h.stub.Cancel(
            assistant_pb2.CancelRequest(turn_id="t-cancel"), metadata=_auth(_token())
        )
        events = await h.chat(_token(), message="late cancellation fee", turn="t-cancel")
    assert kinds(events) == ["done"] and events[0].done.reason == "cancelled"


# -- threads --------------------------------------------------------------------------------


async def _list(h: Harness, token: str):
    reply = await h.stub.ListConversations(
        assistant_pb2.ListConversationsRequest(), metadata=_auth(token)
    )
    return list(reply.conversations)


async def test_each_conversation_is_its_own_thread_listed_newest_first():
    """Referenced by tests/cases/pb-007-assistant-keeps-threads-per-user.yaml."""
    async with Harness(FakePartA.seeded()) as h:
        token = _token()
        await h.chat(token, conversation="first", message="is there a late cancellation fee")
        await h.chat(token, conversation="second", starter="book")
        threads = await _list(h, token)
    assert [t.conversation_id for t in threads] == ["second", "first"]
    assert threads[0].title == "Book an appointment" and threads[0].awaiting_input
    assert threads[1].title == "is there a late cancellation fee" and not threads[1].awaiting_input


async def test_a_reopened_thread_has_every_turn_and_its_open_question_still_answers():
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token()
        events = await h.chat(token, conversation="t", starter="book")
        specialties = last(events, "options")
        await h.chat(token, conversation="t", choice=(specialties.options_id, "Cardiology"))
        reply = await h.stub.GetConversation(
            assistant_pb2.GetConversationRequest(conversation_id="t"), metadata=_auth(token)
        )
        doctors = [e.options for e in reply.turns[-1].events if e.WhichOneof("kind") == "options"][
            -1
        ]
        # Days later, from the reopened thread: the paused flow resumes where it stopped.
        resumed = await h.chat(token, conversation="t", choice=(doctors.options_id, "any"))
    assert [t.user_text for t in reply.turns] == ["Book an appointment", "Cardiology"]
    assert reply.turns[-1].events[-1].done.reason == "awaiting_input"
    assert last(resumed, "options").prompt == "Which time suits you?"


async def test_one_users_threads_are_invisible_to_another():
    async with Harness(FakePartA.seeded()) as h:
        await h.chat(_token(), conversation="private", message="late cancellation fee")
        other = _token(user="u-other")
        assert await _list(h, other) == []
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await h.stub.GetConversation(
                assistant_pb2.GetConversationRequest(conversation_id="private"),
                metadata=_auth(other),
            )
    assert raised.value.code() is grpc.StatusCode.NOT_FOUND


async def test_a_deleted_thread_is_gone_with_its_paused_flow():
    async with Harness(FakePartA.seeded()) as h:
        token = _token()
        events = await h.chat(token, conversation="gone", starter="book")
        specialties = last(events, "options")
        deleted = await h.stub.DeleteConversation(
            assistant_pb2.DeleteConversationRequest(conversation_id="gone"), metadata=_auth(token)
        )
        after = await h.chat(
            token, conversation="gone", choice=(specialties.options_id, "Cardiology")
        )
        threads = await _list(h, token)
    assert deleted.deleted
    assert last(after, "error").code == "stale"
    assert [t.conversation_id for t in threads] == ["gone"]  # the stale reply is a new turn


# -- usage ----------------------------------------------------------------------------------


async def test_admin_sees_llm_usage_per_user_and_nobody_else_does():
    """Referenced by tests/cases/pb-008-admin-sees-llm-usage-per-user.yaml."""
    model = Scripted(
        script=[
            AIMessage(
                content="Only water beforehand.",
                usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
            ),
        ]
    )
    async with Harness(FakePartA.seeded(), model=model) as h:
        patient = _token()
        await h.chat(patient, conversation="a", message="can I eat before a fasting blood test?")
        await h.chat(patient, conversation="b", starter="my_appointments")  # a flow: no model
        desk = _token(UserRole.FRONT_DESK, user="u-desk")
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await h.stub.Usage(assistant_pb2.UsageRequest(), metadata=_auth(desk))
        reply = await h.stub.Usage(
            assistant_pb2.UsageRequest(), metadata=_auth(_token(UserRole.ADMIN, user="u-admin"))
        )
    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
    [row] = reply.users
    assert (row.user_id, row.role) == ("u-patient", "patient")
    assert (row.conversations, row.turns, row.model_turns) == (2, 2, 1)
    assert (row.input_tokens, row.output_tokens) == (1000, 200)
    assert row.cost_usd > 0 and row.errors == 0 and row.last_active
    assert reply.totals.input_tokens == 1000


async def test_usage_rejects_a_bad_date():
    async with Harness(FakePartA.seeded()) as h:
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await h.stub.Usage(
                assistant_pb2.UsageRequest(from_date="last week"),
                metadata=_auth(_token(UserRole.ADMIN, user="u-admin")),
            )
    assert raised.value.code() is grpc.StatusCode.INVALID_ARGUMENT


# -- drafts a person approves -------------------------------------------------------------------


def _booked_with(fake: FakePartA, provider: dict, *, note_status: str | None = None) -> str:
    """An appointment with `provider` tomorrow, optionally with a clinical note."""
    slot = next(s for s in fake.slots if s["provider_id"] == provider["id"])
    appointment_id = str(uuid.uuid4())
    fake.appointments[appointment_id] = {
        "id": appointment_id,
        "status": "confirmed",
        "patient_id": fake.patient["id"],
        "provider_id": provider["id"],
        "slot_id": slot["id"],
        "starts_at": slot["starts_at"],
        "reason": None,
        "in_progress": False,
    }
    if note_status:
        fake.notes[appointment_id] = {
            "id": str(uuid.uuid4()),
            "appointment_id": appointment_id,
            "version": 1,
            "status": note_status,
            "presenting_complaint": "Sore throat for five days",
            "findings": "Inflamed tonsils, no fever",
            "clinician_notes": "Recheck if no better",
            "patient_summary": "",
            "diagnoses": [{"description": "Tonsillitis", "icd10": None}],
            "prescriptions": [
                {
                    "medication": "Amoxicillin",
                    "dose": "500 mg",
                    "frequency": "three times a day",
                    "duration": "7 days",
                    "instructions": "Take with food.",
                }
            ],
            "orders": [],
        }
    return appointment_id


async def test_front_desk_drafts_edits_and_sends_a_notice_only_on_confirmation():
    """Referenced by tests/cases/pb-009-drafts-are-sent-only-after-approval.yaml."""
    fake = FakePartA.seeded()
    ada = fake.providers[0]
    _booked_with(fake, ada)
    async with Harness(fake) as h:
        desk = _token(UserRole.FRONT_DESK, user="u-desk")
        events = await h.chat(desk, starter="notify_patients")
        events = await h.chat(desk, choice=(last(events, "options").options_id, ada["id"]))
        events = await h.chat(desk, choice=(last(events, "options").options_id, "7"))
        assert "should this patient be told" in text(events)
        events = await h.chat(desk, message="Dr Lovelace is away next week.")
        draft = last(events, "card")
        assert draft.badge == "Not sent" and fake.writes == []
        review = last(events, "options")
        events = await h.chat(desk, choice=(review.options_id, "edit"))
        events = await h.chat(desk, message="Dr Lovelace is away. Please pick a new time.")
        assert "Dr Lovelace is away. Please pick a new time." in {
            f.value for f in last(events, "card").fields
        }
        events = await h.chat(desk, choice=(last(events, "options").options_id, "send"))
        confirmation = last(events, "confirmation")
        assert fake.writes == []  # nothing sent until the yes
        events = await h.confirm(desk, confirmation.confirmation_id, True)
        usage = await h.stub.Usage(
            assistant_pb2.UsageRequest(), metadata=_auth(_token(UserRole.ADMIN, user="u-admin"))
        )
    [(method, path, body, headers)] = fake.writes
    assert (method, path) == ("POST", "/notifications")
    assert body["patient_id"] == fake.patient["id"]
    assert body["body"] == "Dr Lovelace is away. Please pick a new time."
    assert headers["authorization"] == f"Bearer {desk}"
    assert headers["idempotency-key"].endswith(f":{fake.patient['id']}")
    assert last(events, "card").badge == "Sent"
    assert (usage.drafts_generated, usage.drafts_sent, usage.draft_recipients) == (1, 1, 1)


async def test_a_discarded_notice_sends_nothing():
    fake = FakePartA.seeded()
    ada = fake.providers[0]
    _booked_with(fake, ada)
    async with Harness(fake) as h:
        desk = _token(UserRole.FRONT_DESK, user="u-desk")
        events = await h.chat(desk, starter="notify_patients")
        events = await h.chat(desk, choice=(last(events, "options").options_id, ada["id"]))
        events = await h.chat(desk, choice=(last(events, "options").options_id, "1"))
        events = await h.chat(desk, message="The clinic opens late tomorrow.")
        events = await h.chat(desk, choice=(last(events, "options").options_id, "discard"))
    assert "Nothing was sent" in text(events) and fake.writes == []


async def test_a_provider_saves_model_written_instructions_into_the_draft_note():
    fake = FakePartA.seeded()
    ada = fake.providers[0]
    appointment_id = _booked_with(fake, ada, note_status="draft")
    written = "Take amoxicillin 500 mg three times a day for 7 days, with food."
    model = Scripted(
        script=[
            AIMessage(
                content=written,
                usage_metadata={"input_tokens": 300, "output_tokens": 40, "total_tokens": 340},
            )
        ]
    )
    async with Harness(fake, model=model) as h:
        doctor_token = _token(UserRole.PROVIDER, user="u-ada")
        events = await h.chat(doctor_token, starter="patient_instructions")
        events = await h.chat(
            doctor_token, choice=(last(events, "options").options_id, appointment_id)
        )
        assert {f.value for f in last(events, "card").fields} == {written}
        prompt = model.calls[0]
        assert "never add a diagnosis" in prompt[0].content and "Tonsillitis" in prompt[1].content
        events = await h.chat(doctor_token, choice=(last(events, "options").options_id, "save"))
        events = await h.confirm(doctor_token, last(events, "confirmation").confirmation_id, True)
    note = fake.notes[appointment_id]
    assert note["patient_summary"] == written
    assert note["clinician_notes"] == "Recheck if no better" and note["status"] == "draft"
    assert "sign when you're ready" in text(events)


async def test_a_signed_note_is_not_rewritten():
    fake = FakePartA.seeded()
    appointment_id = _booked_with(fake, fake.providers[0], note_status="signed")
    async with Harness(fake) as h:
        doctor_token = _token(UserRole.PROVIDER, user="u-ada")
        events = await h.chat(doctor_token, starter="patient_instructions")
        events = await h.chat(
            doctor_token, choice=(last(events, "options").options_id, appointment_id)
        )
    assert "Amend it first" in text(events) and fake.writes == []


# -- reports written in the background ----------------------------------------------------------


async def _watch(h: Harness, token: str, report_id: str, resume: int = 0):
    request = assistant_pb2.WatchReportRequest(report_id=report_id, resume_from=resume)
    return [e async for e in h.stub.WatchReport(request, metadata=_auth(token))]


async def test_an_admin_report_runs_in_the_background_and_streams_figures_then_narrative():
    """Referenced by tests/cases/pb-010-reports-stream-from-the-background.yaml."""
    narrative = "Bookings rose while cancellations doubled."
    model = Scripted(
        script=[
            AIMessage(
                content=narrative,
                usage_metadata={"input_tokens": 500, "output_tokens": 60, "total_tokens": 560},
            )
        ]
    )
    fake = FakePartA.seeded()
    async with Harness(fake, model=model) as h:
        admin = _token(UserRole.ADMIN, user="u-admin")
        job = await h.stub.StartReport(
            assistant_pb2.StartReportRequest(kind="operations_7"), metadata=_auth(admin)
        )
        assert job.status == "running"  # answered before the report is written
        events = await _watch(h, admin, job.report_id)
        again = await _watch(h, admin, job.report_id, resume=2)  # from the record, resumable
        listed = await h.stub.ListReports(assistant_pb2.ListReportsRequest(), metadata=_auth(admin))
        usage = await h.stub.Usage(assistant_pb2.UsageRequest(), metadata=_auth(admin))
    card = last(events, "card")
    figures = {f.label: f.value for f in card.fields}
    assert figures["Appointments booked"].startswith("48 (+20%")  # 48 vs 40, from Part A
    assert figures["Cancellations"].startswith("6 (+100%")
    assert narrative in text(events) and events[-1].done.reason == "complete"
    assert [e.seq for e in again] == [e.seq for e in events if e.seq > 2]
    assert [(r.report_id, r.status) for r in listed.reports] == [(job.report_id, "done")]
    assert usage.users[0].input_tokens == 500  # the report's model call is counted
    # The two windows are asked by Part A's own parameter names, and they differ.
    windows = [(c["from"], c["to"]) for c in fake.analytics_calls]
    assert len(set(windows)) == 2 and windows[0][1] > windows[1][1]


async def test_reports_are_for_admins_only():
    async with Harness(FakePartA.seeded()) as h:
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await h.stub.StartReport(
                assistant_pb2.StartReportRequest(kind="operations_7"),
                metadata=_auth(_token(UserRole.FRONT_DESK, user="u-desk")),
            )
    assert raised.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def test_a_report_without_part_a_fails_and_says_why():
    fake = FakePartA.seeded()
    fake.down = True
    async with Harness(fake) as h:
        admin = _token(UserRole.ADMIN, user="u-admin")
        job = await h.stub.StartReport(
            assistant_pb2.StartReportRequest(kind="operations_30"), metadata=_auth(admin)
        )
        events = await _watch(h, admin, job.report_id)
        listed = await h.stub.ListReports(assistant_pb2.ListReportsRequest(), metadata=_auth(admin))
    assert "Couldn't fetch the figures" in last(events, "error").message
    assert listed.reports[0].status == "failed"


async def test_the_snapshot_asks_part_a_for_two_different_weeks():
    """Regression: the window was sent as start/end, which Part A ignores (it names them
    from/to), so "this week" and "last week" were both the default 30 days."""
    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        await h.chat(_token(UserRole.ADMIN, user="u-admin"), starter="snapshot")
    windows = [(c.get("from"), c.get("to")) for c in fake.analytics_calls]
    assert len(windows) == 2 and None not in windows[0] and windows[0] != windows[1]


# -- metrics for the Assistant dashboard ----------------------------------------------------------


def _metric_points(name: str) -> list:
    from tests.harness.telemetry import METRIC_READER

    data = METRIC_READER.get_metrics_data()
    return [
        p
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for p in m.data.data_points
    ]


async def test_a_turn_is_counted_on_the_dashboard_without_naming_the_user():
    model = Scripted(
        script=[
            AIMessage(
                content="Water only.",
                usage_metadata={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
            )
        ]
    )
    before = sum(p.value for p in _metric_points("assistant.llm.tokens"))
    async with Harness(FakePartA.seeded(), model=model) as h:
        await h.chat(_token(), message="can I eat before a fasting blood test?")
    tokens = _metric_points("assistant.llm.tokens")
    assert sum(p.value for p in tokens) - before == 100
    turns = _metric_points("assistant.turns")
    labels = set().union(*(p.attributes.keys() for p in turns))
    assert labels <= {"role", "outcome", "model_used", "kind"}  # never a user or turn id
    assert any(
        p.attributes["role"] == "patient" and p.attributes["model_used"] == "true" for p in turns
    )


# -- booking conversion, from appointments.booked ------------------------------------------------


def _booked_event(appointment_id: str, origin: str, conversation_ref: str | None) -> dict:
    """appointments.booked as the Avro deserializer hands it over (Part A's schema)."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "appointments.booked",
        "schema_version": 1,
        "occurred_at": datetime.now(UTC),
        "producer": "part-a",
        "subject_id": appointment_id,
        "payload": {
            "appointment_id": appointment_id,
            "origin": origin,
            "conversation_ref": conversation_ref,
        },
    }


async def test_conversion_counts_bookings_that_came_from_a_conversation():
    """Referenced by tests/cases/pb-011-booking-conversion-from-kafka.yaml."""
    from assistant_service.main import apply_booking

    fake = FakePartA.seeded()
    async with Harness(fake) as h:
        token = _token()
        confirmation, _ = await _book_until_confirmation(h, token, conversation="chat-1")
        events = await h.confirm(token, confirmation.confirmation_id, True, conversation="chat-1")
        await h.chat(token, conversation="chat-2", message="late cancellation fee")  # no booking
        appointment_id = last(events, "card").link.rsplit("/", 1)[1]
        usage = h._server.servicer._usage
        booked = _booked_event(appointment_id, "assistant", "chat-1")
        await apply_booking(usage, booked)
        await apply_booking(usage, booked)  # redelivered: counted once
        await apply_booking(usage, _booked_event(str(uuid.uuid4()), "front_desk", None))
        await apply_booking(usage, _booked_event(str(uuid.uuid4()), "assistant", "not-ours"))
        reply = await h.stub.Usage(
            assistant_pb2.UsageRequest(), metadata=_auth(_token(UserRole.ADMIN, user="u-admin"))
        )
    assert (reply.bookings_total, reply.bookings_via_assistant) == (3, 2)
    # A conversation ref this service never had a turn in is not a conversion of ours.
    assert (reply.conversations_with_booking, reply.conversations) == (1, 2)
