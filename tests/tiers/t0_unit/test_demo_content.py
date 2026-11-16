"""The demo's clinical content: every documented specialty has cases, and every test code
exists in the real catalogue -- an imported order with an unknown code would break the
patient record's preparation guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.demo.content import CANCELLATION_REASONS, CONTENT, RESULT_TEXT, SPECIALTIES
from app.modules.clinical.catalogue import CATALOGUE
from app.modules.scheduling.models import Appointment

pytestmark = pytest.mark.unit

# This demo's own bounds (the diagnoses JSON column has no schema).
DIAGNOSIS_TEXT_MAX = 500
ICD10_MAX = 10
CANCELLATION_MAX = Appointment.__table__.c.cancellation_reason.type.length

CORPUS = Path(__file__).resolve().parents[3] / "services/knowledge/corpus/specialties.md"


def test_every_specialty_has_at_least_two_cases():
    assert set(CONTENT) == set(SPECIALTIES)
    assert len(SPECIALTIES) == 10
    assert all(len(cases) >= 2 for cases in CONTENT.values())


def test_specialties_match_the_knowledge_corpus():
    headings = {
        line[len("## ") :].strip()
        for line in CORPUS.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }
    assert set(SPECIALTIES) == headings


def test_every_case_is_signable():
    # A note is signed only with at least one diagnosis and a patient summary
    # (app/modules/clinical/service.py:sign).
    for cases in CONTENT.values():
        for case in cases:
            assert case.diagnoses and case.summary.strip()
            for description, icd10 in case.diagnoses:
                assert 0 < len(description) <= DIAGNOSIS_TEXT_MAX
                assert 0 < len(icd10) <= ICD10_MAX


def test_test_codes_exist_and_have_a_result_text():
    codes = {code for cases in CONTENT.values() for case in cases for code in case.tests}
    assert codes <= set(CATALOGUE)
    assert codes <= set(RESULT_TEXT)


def test_cancellation_reasons_fit_the_column():
    assert CANCELLATION_REASONS and all(len(r) <= CANCELLATION_MAX for r in CANCELLATION_REASONS)
