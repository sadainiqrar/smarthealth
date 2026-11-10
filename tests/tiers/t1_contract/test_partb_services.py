"""The Part B skeletons over real gRPC, in process: knowledge and assistant servers on
ephemeral ports, a token from Part A's real signing code verified by each service against
a JWKS it fetched, and no Part A process at all (spec section 1.2: a Part B service's
tests run with Part A absent).

Contract tier: no containers. The stores are the in-memory implementations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import grpc
import pytest

import tests.harness.partb  # noqa: F401 - the services' import paths
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token, generate_signing_key, public_jwk
from app.settings import Settings

from assistant_service.auth import TokenVerifier as AssistantVerifier  # noqa: E402
from assistant_service.server import build_server as build_assistant  # noqa: E402
from assistant_service.stores import InMemoryStores  # noqa: E402
from knowledge_service.auth import TokenVerifier as KnowledgeVerifier  # noqa: E402
from knowledge_service.consumer import apply  # noqa: E402
from knowledge_service.server import build_server as build_knowledge  # noqa: E402
from knowledge_service.store import InMemoryDirectory  # noqa: E402
from smarthealth.assistant.v1 import assistant_pb2, assistant_pb2_grpc  # noqa: E402
from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc  # noqa: E402

pytestmark = pytest.mark.contract

PART_A = Settings(jwt_private_key=generate_signing_key())
ISSUER = PART_A.jwt_issuer


def _jwks(_url):
    return {"keys": [public_jwk(PART_A)]}


def _token(role=UserRole.PATIENT, *, user="u1", settings=PART_A, issued=None) -> str:
    return create_access_token(subject=user, role=role, session_id="s1", settings=settings,
                               now=issued or datetime.now(UTC))


def _auth(token):
    return (("authorization", f"Bearer {token}"),)


def _state(provider_id, *, specialty="cardiology", active=True):
    return {"payload": {"provider_id": provider_id, "first_name": "Ada", "last_name": "Lovelace",
                        "specialty": specialty, "license_number": "L1", "is_active": active,
                        "department_ids": []}}


@pytest.fixture
async def knowledge():
    directory = InMemoryDirectory()
    server = build_knowledge(directory, KnowledgeVerifier("jwks", ISSUER, fetch=_jwks), 0)
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{server.port}")
    yield directory, knowledge_pb2_grpc.KnowledgeStub(channel), channel
    await channel.close()
    await server.stop(None)


@pytest.fixture
async def assistant(knowledge):
    _, _, knowledge_channel = knowledge
    stores = InMemoryStores()
    server = build_assistant(stores, AssistantVerifier("jwks", ISSUER, fetch=_jwks),
                             knowledge_channel, 0, token_delay=0)
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{server.port}")
    yield stores, assistant_pb2_grpc.AssistantStub(channel)
    await channel.close()
    await server.stop(None)


async def test_knowledge_answers_from_its_own_copy_built_from_provider_state(knowledge):
    """Referenced by tests/cases/pb-001-knowledge-serves-its-own-copy-of-providers.yaml."""
    directory, stub, _ = knowledge
    active, gone = str(uuid.uuid4()), str(uuid.uuid4())
    await apply(directory, active, _state(active))
    await apply(directory, gone, _state(gone, active=False))  # a deactivated provider
    reply = await stub.Search(knowledge_pb2.SearchRequest(query="cardio"), metadata=_auth(_token()))
    assert [hit.source_id for hit in reply.hits] == [f"provider:{active}"]
    await apply(directory, active, None)  # a tombstone removes it
    reply = await stub.Search(knowledge_pb2.SearchRequest(query="cardio"), metadata=_auth(_token()))
    assert list(reply.hits) == []


@pytest.mark.parametrize("token", [None, "garbage"])
async def test_knowledge_refuses_a_missing_or_garbage_token(knowledge, token):
    _, stub, _ = knowledge
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await stub.Search(knowledge_pb2.SearchRequest(query="x"),
                          metadata=_auth(token) if token else ())
    assert raised.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_knowledge_refuses_a_token_part_a_did_not_sign(knowledge):
    _, stub, _ = knowledge
    forged = _token(settings=Settings(jwt_private_key=generate_signing_key()))
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await stub.Search(knowledge_pb2.SearchRequest(query="x"), metadata=_auth(forged))
    assert raised.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def _chat(stub, token, *, turn="t1", message="chest pain cardio", resume=0):
    request = assistant_pb2.ChatRequest(conversation_id="c1", turn_id=turn, message=message,
                                        resume_from=resume)
    return [event async for event in stub.Chat(request, metadata=_auth(token))]


async def test_a_chat_turn_streams_tokens_then_citations_then_done(knowledge, assistant):
    """Referenced by tests/cases/pb-002-a-chat-turn-streams-and-resumes.yaml."""
    directory, _, _ = knowledge
    provider = str(uuid.uuid4())
    await apply(directory, provider, _state(provider))
    _, stub = assistant
    events = await _chat(stub, _token(), message="cardio")
    kinds = [event.WhichOneof("kind") for event in events]
    assert kinds == ["token", "token", "token", "citation", "done"]
    text = "".join(e.token for e in events if e.WhichOneof("kind") == "token")
    assert text.strip() == "You said: cardio"
    assert events[3].citation.source_id == f"provider:{provider}"
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


async def test_a_reconnect_resumes_after_the_last_seq_it_saw(knowledge, assistant):
    _, stub = assistant
    first = await _chat(stub, _token(), turn="t-resume", message="one two three")
    resumed = await _chat(stub, _token(), turn="t-resume", message="ignored", resume=3)
    assert [e.seq for e in resumed] == [e.seq for e in first if e.seq > 3]
    assert resumed[-1].done.reason == "complete"


async def test_a_cancelled_turn_ends_with_done_cancelled(knowledge, assistant):
    stores, stub = assistant
    await stub.Cancel(assistant_pb2.CancelRequest(turn_id="t-cancel"), metadata=_auth(_token()))
    events = await _chat(stub, _token(), turn="t-cancel")
    assert [e.WhichOneof("kind") for e in events] == ["done"]
    assert events[0].done.reason == "cancelled"


async def test_a_token_issued_before_a_revocation_is_refused(knowledge, assistant):
    """Referenced by tests/cases/pb-003-a-revoked-login-is-refused-by-part-b.yaml.

    The assistant never reads Part A's sessions: identity.sessions_revoked fills its own
    deny-list, and a token issued before the revocation is refused though its signature is
    good. A token issued after (a fresh login) works."""
    stores, stub = assistant
    old = _token(user="u-revoked", issued=datetime.now(UTC) - timedelta(minutes=2))
    await stores.revoke("u-revoked", (datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await _chat(stub, old)
    assert raised.value.code() is grpc.StatusCode.UNAUTHENTICATED
    fresh = _token(user="u-revoked")
    assert (await _chat(stub, fresh, turn="t-fresh"))[-1].done.reason == "complete"


async def test_the_assistant_degrades_when_knowledge_is_down():
    """Spec section 9.2: no knowledge means an answer without citations, not a failure."""
    dead = grpc.aio.insecure_channel("localhost:1")  # nothing listens there
    server = build_assistant(InMemoryStores(), AssistantVerifier("jwks", ISSUER, fetch=_jwks),
                             dead, 0, token_delay=0)
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{server.port}")
    try:
        events = await _chat(assistant_pb2_grpc.AssistantStub(channel), _token(), turn="t-dead")
    finally:
        await channel.close()
        await server.stop(None)
        await dead.close()
    assert events[-1].done.reason == "complete"
    assert "citation" not in [e.WhichOneof("kind") for e in events]
