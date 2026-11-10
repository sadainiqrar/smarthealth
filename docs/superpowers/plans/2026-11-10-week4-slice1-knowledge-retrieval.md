# Week 4 slice 1 — Knowledge ingestion and hybrid retrieval: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans.

**Goal:** `knowledge.Search` answers from a real index: a committed fictional MediNova corpus
(Markdown and PDF) and the provider directory are parsed, split by heading, embedded
(dense + BM25 sparse) and stored in Qdrant; retrieval is hybrid with audience filtering
from the caller's token and a relevance floor; a golden set measures it.

**Spec:** Weeks 3–5 spec §8 (D-29), harness requirement 2 (provider factory on `llm_mode`).

## Decisions

1. **Embeddings run locally with fastembed** (`BAAI/bge-small-en-v1.5`, 384-d, and
   `Qdrant/bm25`). Behind a factory keyed on mode: `live` embeds; `record` embeds and writes
   each vector to `services/knowledge/eval/fixtures/embeddings.jsonl` keyed by a hash of model
   and text; `fixture` replays that file and fails loudly on a miss. CI and every test run
   in `fixture`: real model vectors, no download, the same answer every time.
2. **Hybrid retrieval in one Qdrant query**: dense and sparse prefetch (20 each), fused by
   reciprocal rank, filtered by audience. A dense-similarity floor decides "nothing relevant"
   before fusion — RRF scores are ranks, not relevance.
3. **Chunks**: Markdown split by headings (LangChain `MarkdownHeaderTextSplitter`), then by
   size (~1200 characters, 150 overlap); a PDF's text is split by size with its title as the
   section; one provider is one chunk. Ids are `uuid5(document, version, position)`; a chunk
   whose `content_hash` is unchanged is not re-embedded; a new version is upserted before the
   old version's points are deleted.
4. **Ingestion paths**: the corpus by `python -m knowledge_service.ingest` (and at start-up if
   the collection is empty); providers by the existing `providers.state` consumer, now also
   indexing. An upload RPC with a Celery worker in the knowledge service is deferred: nothing
   uploads documents yet, and the pipeline it would call is this one.
5. **Tests use Qdrant's local mode** (`QdrantClient(":memory:")`) in the contract tier and the
   real container in the docker lane.

## Tasks

- [ ] Corpus: eight Markdown documents and one generated PDF, each declaring id, title,
  audience, kind, version.
- [ ] `parsing.py`, `chunking.py`, `embeddings.py` (factory + fixture file), `index.py`
  (collection, upsert/replace), `retrieval.py` (hybrid, audience, floor).
- [ ] `ingest.py` CLI; providers consumer indexes; `Search` uses retrieval.
- [ ] Golden set (`eval/golden.yaml`), `eval.py` (recall@5, MRR), record the fixtures.
- [ ] Tests: chunking, ids/idempotency, version replacement, audience filter, floor, golden
  bars; T3 against the Qdrant container.
- [ ] Compose (qdrant already there), metrics, cases, docs.
