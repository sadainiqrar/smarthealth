"""Measuring retrieval against the golden set: recall@k and MRR (spec section 8.5).

    python -m knowledge_service.eval

recall@k: the share of answerable questions with an acceptable passage in the top k.
MRR: the mean of 1/rank of the first acceptable passage (0 when none is found) -- it rewards
putting the right passage first, which is what the assistant reads most. Unanswerable
questions (`expect: []`) score on whether nothing came back.

Run with `KNOWLEDGE_EMBEDDING_MODE=record` once after changing the corpus, the golden set or
the model: it embeds for real and writes the fixture file the test suite replays.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from knowledge_service.index import Index

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden.yaml"
K = 5

#: Texts the test suite embeds beyond the corpus and the golden questions: recorded with
#: them, so fixture mode has them too.
EXTRA_DOCUMENTS = ["Ada Lovelace, cardiology clinician at MediNova"]
EXTRA_QUERIES = ["cardiology clinician", "physiotherapy referral process"]


@dataclass
class Report:
    answerable: int = 0
    found_at_k: int = 0
    reciprocal_ranks: list[float] = field(default_factory=list)
    unanswerable: int = 0
    correctly_empty: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.found_at_k / self.answerable if self.answerable else 0.0

    @property
    def mrr(self) -> float:
        ranks = self.reciprocal_ranks
        return sum(ranks) / len(ranks) if ranks else 0.0

    @property
    def empty_accuracy(self) -> float:
        return self.correctly_empty / self.unanswerable if self.unanswerable else 1.0


def audiences_for(audience: str) -> list[str]:
    return ["patient"] if audience == "patient" else ["patient", "staff"]


async def evaluate(index: Index, golden: Path = GOLDEN) -> Report:
    report = Report()
    for case in yaml.safe_load(golden.read_text(encoding="utf-8")):
        hits = await index.search(case["q"], audiences=audiences_for(case["audience"]), limit=K)
        ranked = [hit.source_id for hit in hits]
        expected = set(case["expect"])
        if not expected:
            report.unanswerable += 1
            report.correctly_empty += not ranked
            if ranked:
                report.misses.append(f"expected nothing for {case['q']!r}, got {ranked[:2]}")
            continue
        report.answerable += 1
        rank = next((i + 1 for i, source in enumerate(ranked) if source in expected), None)
        report.found_at_k += rank is not None
        report.reciprocal_ranks.append(1 / rank if rank else 0.0)
        if rank is None:
            report.misses.append(f"{case['q']!r}: wanted {sorted(expected)}, got {ranked}")
    return report


async def _main() -> None:
    from qdrant_client import AsyncQdrantClient

    from knowledge_service.embeddings import embedder_for
    from knowledge_service.ingest import ingest_corpus
    from knowledge_service.settings import KnowledgeSettings

    settings = KnowledgeSettings()
    client = AsyncQdrantClient(location=":memory:")
    index = Index(client, embedder_for(settings.embedding_mode))
    await ingest_corpus(index)
    if settings.embedding_mode == "record":
        index._embedder.documents(EXTRA_DOCUMENTS)
        for text in EXTRA_QUERIES:
            index._embedder.query(text)
    report = await evaluate(index)
    print(f"recall@{K} {report.recall:.2f}  MRR {report.mrr:.2f}  "
          f"empty when nothing is relevant {report.empty_accuracy:.2f}")
    for miss in report.misses:
        print("  miss:", miss)
    await client.close()


if __name__ == "__main__":
    asyncio.run(_main())
