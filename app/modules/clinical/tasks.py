"""Extracting text from an uploaded report, off the request (a Celery task).

PDFs with a text layer are read with pypdf; images and scanned PDFs are stored and shown
but not extracted -- OCR is out of scope (spec 9.6). The extracted text is what the
assistant reads to explain a report to its patient or summarise it for a clinician.
Idempotent: running it again rewrites the same text.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import TASKS, celery, task_settings
from app.modules.clinical import service
from app.modules.clinical.models import PatientDocument
from app.modules.clinical.storage import FilesystemDocumentStore
from app.settings import Settings

logger = logging.getLogger(__name__)
#: Extracted text kept per document; a report longer than this is truncated.
MAX_TEXT = 200_000


def extract_text(data: bytes, media_type: str) -> tuple[str, str | None]:
    if media_type != "application/pdf":
        return "not_extractable", None
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 - a malformed PDF is a document we cannot read
        logger.warning("could not read the PDF", exc_info=True)
        return "failed", None
    text = text.strip()
    return ("extracted", text[:MAX_TEXT]) if text else ("not_extractable", None)


async def extract(settings: Settings, document_id: uuid.UUID, store=None) -> str:
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = store or FilesystemDocumentStore.from_settings(settings)
    try:
        async with sessions() as session:
            document = await session.get(PatientDocument, document_id)
            if document is None:
                return "missing"
            data = await store.get(document.object_key)
            status, text = extract_text(data, document.media_type)
            await service.set_extraction(session, document_id, status=status, text=text)
            await session.commit()
            return status
    finally:
        await engine.dispose()


@celery.task(name="clinical.extract_document", acks_late=True, autoretry_for=(OSError,),
             retry_backoff=True, max_retries=5)
def extract_document(document_id: str) -> str:
    return asyncio.run(extract(task_settings(), uuid.UUID(document_id)))


def enqueue_extraction(document_id: str) -> None:
    celery.send_task("clinical.extract_document", args=[document_id],
                     queue=task_settings().queue("documents"))


TASKS.register("clinical.extract_document", extract_document)
