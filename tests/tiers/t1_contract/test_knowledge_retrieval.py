"""Knowledge retrieval in fixture mode: real recorded embeddings, Qdrant's local mode, no
network and no model download (spec section 8.5; harness requirement 2).

The golden-set bars are the Week 4 deliverable ("relevant retrieval pipeline working"),
measured rather than eyeballed. The other tests pin the properties that keep retrieval
honest as the corpus changes: the audience wall, the relevance floor, idempotent and
versioned ingestion.
"""

from __future__ import annotations

import shutil

import grpc
import pytest

import tests.harness.partb  # noqa: F401
from knowledge_service.chunking import chunk_document, chunk_provider  # noqa: E402
from knowledge_service.documents import read_corpus  # noqa: E402
from knowledge_service.embeddings import FixtureEmbedder, FixtureMiss  # noqa: E402
from knowledge_service.eval import evaluate  # noqa: E402
from knowledge_service.index import Index  # noqa: E402
from knowledge_service.ingest import CORPUS, ingest_corpus  # noqa: E402
from qdrant_client import AsyncQdrantClient, models  # noqa: E402

pytestmark = pytest.mark.contract

#: The bars. Measured 2026-11-10 on the recorded vectors: recall@5 0.97, MRR 0.94, every
#: off-topic question empty. Set a little below, so a small corpus edit does not fail them
#: but a broken pipeline does.
MIN_RECALL = 0.9
MIN_MRR = 0.8


@pytest.fixture
async def index():
    client = AsyncQdrantClient(location=":memory:")
    built = Index(client, FixtureEmbedder())
    await ingest_corpus(built)
    yield built
    await client.close()


async def test_the_golden_set_meets_its_bars(index):
    """Referenced by tests/cases/kn-001-retrieval-meets-the-golden-set.yaml."""
    report = await evaluate(index)
    assert report.recall >= MIN_RECALL, report.misses
    assert report.mrr >= MIN_MRR, report.misses
    assert report.empty_accuracy == 1.0, report.misses


async def test_a_patient_never_retrieves_a_staff_protocol(index):
    """Referenced by tests/cases/kn-002-the-audience-wall.yaml. Spec 8.4: the audience comes
    from the token. The same question finds the protocol for staff and nothing of it for a
    patient -- not a lower rank, nothing."""
    staff = await index.search("physiotherapy referral process", audiences=["patient", "staff"],
                               limit=5)
    patient = await index.search("physiotherapy referral process", audiences=["patient"], limit=5)
    assert any(h.source_id.startswith("doc:protocol-referrals") for h in staff)
    assert not [h for h in patient if h.source_id.startswith("doc:protocol")]


async def test_an_off_topic_question_returns_nothing_rather_than_a_guess(index):
    assert await index.search("What is the capital of France?", audiences=["patient"],
                              limit=5) == []


async def test_ingesting_again_embeds_nothing_and_duplicates_nothing(index):
    client = index._client
    before = (await client.count(index.collection)).count
    report = await ingest_corpus(index)
    assert report.embedded == 0, "unchanged chunks are not embedded again"
    assert (await client.count(index.collection)).count == before


async def test_a_new_version_replaces_the_old_and_leaves_no_stale_chunk(index, tmp_path):
    """Spec 8.3: upsert the new version, then delete the old -- no gap, no leftovers."""
    shutil.copytree(CORPUS, tmp_path / "corpus")
    faq = tmp_path / "corpus" / "faq.md"
    faq.write_text(faq.read_text(encoding="utf-8").replace("version: 1", "version: 2", 1),
                   encoding="utf-8")
    document = next(d for d in read_corpus(tmp_path / "corpus") if d.id == "faq")
    # Version 2's chunks have new ids; record nothing new -- reuse version 1's vectors by
    # giving the fixture embedder the same texts (the text did not change, only the version).
    await index.replace_document("faq", chunk_document(document))
    points, _ = await index._client.scroll(
        index.collection, with_payload=True, limit=500,
        scroll_filter=models.Filter(must=[models.FieldCondition(
            key="document_id", match=models.MatchValue(value="faq"))]))
    assert {p.payload["version"] for p in points} == {2}


def test_headings_split_sections_and_chunk_ids_are_stable():
    documents = {d.id: d for d in read_corpus(CORPUS)}
    chunks = chunk_document(documents["prep-blood-tests"])
    sections = [c.section for c in chunks]
    assert "Fasting blood test" in sections and "HbA1c" in sections
    assert [c.id for c in chunks] == [c.id for c in chunk_document(documents["prep-blood-tests"])]
    pdf = chunk_document(documents["colonoscopy-preparation"])
    assert pdf and all(c.source_id == "doc:colonoscopy-preparation#preparing-for-a-colonoscopy"
                       for c in pdf)


def test_fixture_mode_refuses_a_text_it_never_recorded():
    with pytest.raises(FixtureMiss):
        FixtureEmbedder().query("a question nobody recorded")


async def test_providers_are_indexed_and_found(index):
    chunk = chunk_provider("p-1", "Ada Lovelace", "cardiology")
    await index.replace_document(chunk.document_id, [chunk])
    hits = await index.search("cardiology clinician", audiences=["patient"], limit=5)
    assert "provider:p-1" in [h.source_id for h in hits]


async def test_search_over_grpc_uses_the_callers_audience(index):
    from datetime import UTC, datetime

    from app.modules.identity.models import UserRole
    from app.modules.identity.security import (
        create_access_token,
        generate_signing_key,
        public_jwk,
    )
    from app.settings import Settings
    from knowledge_service.auth import TokenVerifier
    from knowledge_service.server import build_server
    from knowledge_service.store import InMemoryDirectory
    from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc

    part_a = Settings(jwt_private_key=generate_signing_key())
    server = build_server(InMemoryDirectory(), TokenVerifier(
        "jwks", part_a.jwt_issuer, fetch=lambda _u: {"keys": [public_jwk(part_a)]}), 0, index)
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{server.port}")
    try:
        stub = knowledge_pb2_grpc.KnowledgeStub(channel)

        async def ask(role):
            token = create_access_token(subject="u", role=role, session_id="s",
                                        settings=part_a, now=datetime.now(UTC))
            reply = await stub.Search(
                knowledge_pb2.SearchRequest(query="physiotherapy referral process"),
                metadata=(("authorization", f"Bearer {token}"),))
            return [h.source_id for h in reply.hits]

        assert any(s.startswith("doc:protocol-referrals") for s in await ask(UserRole.PROVIDER))
        assert not [s for s in await ask(UserRole.PATIENT) if s.startswith("doc:protocol")]
    finally:
        await channel.close()
        await server.stop(None)


