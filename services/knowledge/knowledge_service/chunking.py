"""Splitting documents into chunks: by heading first, then by size (spec section 8.2).

Headings are where a document's meaning changes, so a chunk never straddles two sections:
"Fasting blood test" and "HbA1c" are separate chunks even though they sit side by side.
A section longer than `CHUNK_SIZE` is split again with an overlap, so a sentence cut at the
boundary is still whole in one of the two pieces. A PDF has no Markdown headings and is split
by size alone. One provider profile is one chunk.

Each chunk's id is a function of document, version and position (`uuid5`), so ingesting the
same version again overwrites instead of duplicating; and it carries the hash of its text, so
an unchanged chunk is not embedded again.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from knowledge_service.documents import Document

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
NAMESPACE = uuid.UUID("6b1f2c8e-3a4d-4e5f-9a0b-1c2d3e4f5a6b")


@dataclass(frozen=True)
class Chunk:
    id: str
    source_id: str  # "doc:<document>#<section>" or "provider:<id>"
    document_id: str
    version: int
    position: int
    title: str
    section: str
    audience: str
    kind: str
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def _slug(text: str) -> str:
    return "-".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


_by_heading = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=False
)
_by_size = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)


def chunk_document(document: Document) -> list[Chunk]:
    if document.format == "markdown":
        pieces = []
        for section in _by_heading.split_text(document.text):
            heading = section.metadata.get("h3") or section.metadata.get("h2") \
                or section.metadata.get("h1") or document.title
            pieces += [(heading, text) for text in _by_size.split_text(section.page_content)]
    else:
        pieces = [(document.title, text) for text in _by_size.split_text(document.text)]
    chunks = []
    for position, (heading, text) in enumerate(pieces):
        chunks.append(Chunk(
            id=str(uuid.uuid5(NAMESPACE, f"{document.id}:{document.version}:{position}")),
            source_id=f"doc:{document.id}#{_slug(heading)}",
            document_id=document.id, version=document.version, position=position,
            title=document.title, section=heading, audience=document.audience,
            kind=document.kind,
            # The title travels with the text: "Preparing for a blood test -- HbA1c" is what
            # the chunk is about, and a heading alone ("On the day") says too little.
            text=f"{document.title} — {heading}\n{text.strip()}",
        ))
    return chunks


def chunk_provider(provider_id: str, name: str, specialty: str, version: int = 1) -> Chunk:
    """A provider is one chunk, visible to everyone: who they are and what they treat."""
    return Chunk(
        id=str(uuid.uuid5(NAMESPACE, f"provider:{provider_id}")),
        source_id=f"provider:{provider_id}", document_id=f"provider:{provider_id}",
        version=version, position=0, title=name, section=specialty, audience="patient",
        kind="provider", text=f"{name}, {specialty} clinician at MediNova",
    )
