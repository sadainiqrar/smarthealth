"""Ingesting the corpus: parse -> split -> embed -> upsert, one document at a time.

    python -m knowledge_service.ingest [corpus-dir]

Idempotent: each document's chunks replace its previous ones (new version first, then the
old points removed), and chunks whose text did not change are not embedded again. Run it on
a fresh index, after editing the corpus, or after the embedding model changes (bump each
document's version then).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from knowledge_service.chunking import chunk_document
from knowledge_service.documents import read_corpus
from knowledge_service.index import Index

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestReport:
    documents: int
    chunks: int
    embedded: int


async def ingest_corpus(index: Index, directory: Path = CORPUS) -> IngestReport:
    await index.ensure()
    documents = read_corpus(directory)
    chunks = embedded = 0
    for document in documents:
        pieces = chunk_document(document)
        embedded += await index.replace_document(document.id, pieces)
        chunks += len(pieces)
        logger.info("ingested", extra={"document": document.id, "chunks": len(pieces)})
    return IngestReport(len(documents), chunks, embedded)


async def _main(directory: Path) -> None:
    from qdrant_client import AsyncQdrantClient

    from knowledge_service.embeddings import embedder_for
    from knowledge_service.settings import KnowledgeSettings

    settings = KnowledgeSettings()
    client = AsyncQdrantClient(url=settings.qdrant_url)
    report = await ingest_corpus(Index(client, embedder_for(settings.embedding_mode)), directory)
    print(f"{report.documents} documents, {report.chunks} chunks, {report.embedded} embedded")
    await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(Path(sys.argv[1]) if len(sys.argv) > 1 else CORPUS))
