"""The Qdrant index: one collection, two named vectors per point, and hybrid search.

Every chunk is a point with a dense vector and a BM25 sparse vector, and a payload naming its
document, version, section, audience and text. Writes are idempotent (the point id is the
chunk id) and versioned: a new version of a document is upserted *before* the old version's
points are deleted, so a search during re-ingestion finds one version or the other, never a
gap (spec section 8.3).

Search runs both vectors as prefetches and fuses them by reciprocal rank (spec 8.4): a text
that is close in meaning *or* shares the rare exact term ranks high, one that does both ranks
highest. RRF scores are ranks, not relevance, so whether anything is relevant at all is
decided first, on the dense similarity alone (`MIN_SIMILARITY`): below it, the answer is
"nothing", and the assistant says it does not know rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from knowledge_service.chunking import Chunk
from knowledge_service.embeddings import DENSE_SIZE, Embedded, Embedder

COLLECTION = "knowledge"
PREFETCH = 20
#: Cosine similarity of the best dense match below which nothing is returned. Tuned on the
#: golden set: relevant questions sit well above it, off-topic ones below.
MIN_SIMILARITY = 0.55


@dataclass(frozen=True)
class Hit:
    source_id: str
    kind: str
    title: str
    section: str
    text: str
    score: float


class Index:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder,
                 collection: str = COLLECTION) -> None:
        self._client = client
        self._embedder = embedder
        self.collection = collection

    async def ensure(self) -> None:
        if await self._client.collection_exists(self.collection):
            return
        await self._client.create_collection(
            self.collection,
            vectors_config={"dense": models.VectorParams(size=DENSE_SIZE,
                                                         distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        for field in ("audience", "document_id"):
            await self._client.create_payload_index(self.collection, field,
                                                    models.PayloadSchemaType.KEYWORD)

    async def _known_hashes(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        points = await self._client.retrieve(
            self.collection, ids=ids, with_payload=["content_hash"]
        )
        return {str(p.id): p.payload.get("content_hash") for p in points}

    async def upsert(self, chunks: list[Chunk]) -> int:
        """Write chunks; returns how many were embedded (unchanged ones are skipped)."""
        known = await self._known_hashes([c.id for c in chunks])
        changed = [c for c in chunks if known.get(c.id) != c.content_hash]
        if changed:
            vectors = self._embedder.documents([c.text for c in changed])
            await self._client.upsert(self.collection, points=[
                models.PointStruct(id=c.id, vector=_vectors(v), payload={
                    "source_id": c.source_id, "document_id": c.document_id,
                    "version": c.version, "position": c.position, "title": c.title,
                    "section": c.section, "audience": c.audience, "kind": c.kind,
                    "text": c.text, "content_hash": c.content_hash,
                })
                for c, v in zip(changed, vectors, strict=True)
            ])
        return len(changed)

    async def replace_document(self, document_id: str, chunks: list[Chunk]) -> int:
        """This document's chunks become exactly `chunks`: upsert first, then remove every
        point of it that is not among them (an older version, or a section that shrank)."""
        embedded = await self.upsert(chunks)
        keep = [c.id for c in chunks]
        await self._client.delete(self.collection, points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="document_id",
                                            match=models.MatchValue(value=document_id))],
                must_not=[models.HasIdCondition(has_id=keep)],
            )))
        return embedded

    async def remove_document(self, document_id: str) -> None:
        await self._client.delete(self.collection, points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(
                key="document_id", match=models.MatchValue(value=document_id))])))

    async def search(self, query: str, *, audiences: list[str], limit: int) -> list[Hit]:
        embedded = self._embedder.query(query)
        allowed = models.Filter(must=[models.FieldCondition(
            key="audience", match=models.MatchAny(any=audiences))])
        best = await self._client.query_points(
            self.collection, query=embedded.dense, using="dense", query_filter=allowed, limit=1)
        if not best.points or best.points[0].score < MIN_SIMILARITY:
            return []
        fused = await self._client.query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=embedded.dense, using="dense", filter=allowed,
                                limit=PREFETCH),
                models.Prefetch(query=models.SparseVector(indices=embedded.sparse.indices,
                                                          values=embedded.sparse.values),
                                using="bm25", filter=allowed, limit=PREFETCH),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit, with_payload=True,
        )
        return [Hit(p.payload["source_id"], p.payload["kind"], p.payload["title"],
                    p.payload["section"], p.payload["text"], float(p.score))
                for p in fused.points]


def _vectors(embedded: Embedded) -> dict:
    return {"dense": embedded.dense,
            "bm25": models.SparseVector(indices=embedded.sparse.indices,
                                        values=embedded.sparse.values)}
