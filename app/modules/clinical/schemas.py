"""Request and response bodies for the clinical record."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Diagnosis(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    icd10: str | None = Field(default=None, max_length=10)


class PrescriptionIn(BaseModel):
    medication: str = Field(min_length=1, max_length=200)
    dose: str = Field(min_length=1, max_length=100)
    frequency: str = Field(min_length=1, max_length=100)
    duration: str = Field(default="", max_length=100)
    instructions: str = Field(default="", max_length=2000)


class OrderIn(BaseModel):
    test_code: str = Field(min_length=1, max_length=50)
    reason: str = Field(default="", max_length=1000)
    urgency: Literal["routine", "urgent"] = "routine"


class NoteIn(BaseModel):
    presenting_complaint: str = Field(default="", max_length=5000)
    findings: str = Field(default="", max_length=10000)
    clinician_notes: str = Field(default="", max_length=10000)
    patient_summary: str = Field(default="", max_length=5000)
    diagnoses: list[Diagnosis] = Field(default_factory=list, max_length=20)
    prescriptions: list[PrescriptionIn] = Field(default_factory=list, max_length=20)
    orders: list[OrderIn] = Field(default_factory=list, max_length=20)


class NoteRead(BaseModel):
    """A note as its reader may see it: `clinician_notes` is None for a patient."""

    id: uuid.UUID
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    provider_id: uuid.UUID
    version: int
    status: str
    presenting_complaint: str
    findings: str
    clinician_notes: str | None
    patient_summary: str
    diagnoses: list[Diagnosis]
    prescriptions: list[PrescriptionIn]
    orders: list[OrderIn]
    signed_at: datetime | None


class PrescriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    medication: str
    dose: str
    frequency: str
    duration: str
    instructions: str
    status: str


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    test_code: str
    test_name: str
    prep_source_id: str
    urgency: str
    status: str
    created_at: datetime
    #: Front desk sees order status only (spec 9.6): no reason, no result.
    reason: str | None = None
    result_document_id: uuid.UUID | None = None


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    media_type: str
    size_bytes: int
    test_order_id: uuid.UUID | None
    extraction_status: str
    created_at: datetime


class DocumentText(DocumentRead):
    extracted_text: str | None
    #: Whether the ordering clinician has reviewed the result this document answers.
    reviewed: bool


class RecordRead(BaseModel):
    patient_id: uuid.UUID
    notes: list[NoteRead]
    prescriptions: list[PrescriptionRead]
    orders: list[OrderRead]
    documents: list[DocumentRead]
