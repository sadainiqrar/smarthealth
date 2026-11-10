"""Reading corpus documents: Markdown with front matter, and PDF with a sidecar.

A document is its metadata -- id, title, audience (patient or staff), kind, version -- and its
text. The audience is what retrieval filters on (spec section 8.4): a staff protocol is never
returned to a patient's token. PDFs are read with pypdf (text PDFs; scanned ones would need
OCR, which is out of scope) and carry no front matter, so `<file>.pdf.meta.yaml` supplies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pypdf import PdfReader

AUDIENCES = ("patient", "staff")


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    audience: str
    kind: str
    version: int
    text: str
    #: "markdown" or "pdf": decides how the text is split.
    format: str


def _validated(meta: dict, text: str, fmt: str, source: Path) -> Document:
    missing = {"id", "title", "audience", "kind", "version"} - set(meta)
    if missing:
        raise ValueError(f"{source}: missing {sorted(missing)}")
    if meta["audience"] not in AUDIENCES:
        raise ValueError(f"{source}: audience must be one of {AUDIENCES}")
    return Document(str(meta["id"]), str(meta["title"]), meta["audience"], str(meta["kind"]),
                    int(meta["version"]), text, fmt)


def read_markdown(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: no front matter")
    _, front, body = raw.split("---", 2)
    return _validated(yaml.safe_load(front), body.strip(), "markdown", path)


def read_pdf(path: Path) -> Document:
    meta = yaml.safe_load(path.with_name(path.name + ".meta.yaml").read_text(encoding="utf-8"))
    text = "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
    return _validated(meta, text.strip(), "pdf", path)


def read_corpus(directory: Path) -> list[Document]:
    documents = [read_markdown(p) for p in sorted(directory.glob("*.md"))]
    documents += [read_pdf(p) for p in sorted(directory.glob("*.pdf"))]
    ids = [d.id for d in documents]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate document ids: {sorted(duplicates)}")
    return documents
