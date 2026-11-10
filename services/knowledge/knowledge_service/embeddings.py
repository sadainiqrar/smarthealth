"""Embeddings, from a factory keyed on the mode (harness requirement 2; spec section 8.2).

Two embeddings per text, both local (fastembed, no API key, nothing billed):

- **dense** -- `BAAI/bge-small-en-v1.5`, 384 numbers that place texts of similar *meaning*
  near each other ("fasting" near "empty stomach");
- **sparse** -- BM25 term weights, which reward the *exact words* ("HbA1c", "MRI with
  contrast") that meaning-based search can blur.

The mode decides where vectors come from:

- `live`: compute them with the models (downloaded once, then cached);
- `record`: compute them and also write each one to the fixture file;
- `fixture`: replay the fixture file and fail loudly on a text it has never seen.

Tests and CI run in `fixture`: the same vectors the real model produced, with no download and
no variance, so a retrieval test means the same thing on every run.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_SIZE = 384
FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "embeddings.jsonl"


@dataclass(frozen=True)
class Sparse:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class Embedded:
    dense: list[float]
    sparse: Sparse


class Embedder(Protocol):
    def documents(self, texts: list[str]) -> list[Embedded]: ...
    def query(self, text: str) -> Embedded: ...


def _key(kind: str, text: str) -> str:
    return hashlib.sha256(f"{DENSE_MODEL}|{SPARSE_MODEL}|{kind}|{text}".encode()).hexdigest()


class LiveEmbedder:
    """The models. Documents and queries are embedded differently (bge prefixes queries;
    BM25 weighs query terms without document-length normalisation)."""

    def __init__(self) -> None:
        from fastembed import SparseTextEmbedding, TextEmbedding

        self._dense = TextEmbedding(DENSE_MODEL)
        self._sparse = SparseTextEmbedding(SPARSE_MODEL)

    def documents(self, texts: list[str]) -> list[Embedded]:
        dense = list(self._dense.passage_embed(texts))
        sparse = list(self._sparse.passage_embed(texts))
        return [Embedded([float(x) for x in d], Sparse(s.indices.tolist(), s.values.tolist()))
                for d, s in zip(dense, sparse, strict=True)]

    def query(self, text: str) -> Embedded:
        dense = next(iter(self._dense.query_embed(text)))
        sparse = next(iter(self._sparse.query_embed(text)))
        return Embedded([float(x) for x in dense],
                        Sparse(sparse.indices.tolist(), sparse.values.tolist()))


class FixtureMiss(LookupError):
    """A text was embedded in fixture mode that was never recorded. Record it:
    KNOWLEDGE_EMBEDDING_MODE=record python -m knowledge_service.eval"""


class FixtureEmbedder:
    def __init__(self, path: Path = FIXTURES) -> None:
        self._vectors: dict[str, Embedded] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self._vectors[row["key"]] = Embedded(row["dense"], Sparse(**row["sparse"]))

    def _get(self, kind: str, text: str) -> Embedded:
        try:
            return self._vectors[_key(kind, text)]
        except KeyError:
            raise FixtureMiss(f"no recorded {kind} embedding for {text[:60]!r}") from None

    def documents(self, texts: list[str]) -> list[Embedded]:
        return [self._get("document", t) for t in texts]

    def query(self, text: str) -> Embedded:
        return self._get("query", text)


class RecordingEmbedder:
    """Live, and every vector appended to the fixture file (deduplicated by key)."""

    def __init__(self, path: Path = FIXTURES) -> None:
        self._live = LiveEmbedder()
        self._path = path
        self._lock = threading.Lock()
        self._known = set(FixtureEmbedder(path)._vectors)

    def _save(self, kind: str, text: str, embedded: Embedded) -> None:
        key = _key(kind, text)
        with self._lock:
            if key in self._known:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "key": key, "kind": kind, "text": text[:80],
                    "dense": [round(x, 6) for x in embedded.dense],
                    "sparse": {"indices": embedded.sparse.indices,
                               "values": [round(v, 6) for v in embedded.sparse.values]},
                }) + "\n")
            self._known.add(key)

    def documents(self, texts: list[str]) -> list[Embedded]:
        out = self._live.documents(texts)
        for text, embedded in zip(texts, out, strict=True):
            self._save("document", text, embedded)
        return out

    def query(self, text: str) -> Embedded:
        embedded = self._live.query(text)
        self._save("query", text, embedded)
        return embedded


def embedder_for(mode: str) -> Embedder:
    if mode == "live":
        return LiveEmbedder()
    if mode == "record":
        return RecordingEmbedder()
    if mode == "fixture":
        return FixtureEmbedder()
    raise ValueError(f"unknown embedding mode {mode!r}: fixture, record or live")
