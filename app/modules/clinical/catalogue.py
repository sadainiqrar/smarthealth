"""The test catalogue: what a clinician can order, and where its preparation is explained.

Each test names the section of the knowledge corpus that tells the patient how to prepare,
so the assistant can answer "what tests did my doctor ask for, and how do I prepare?" with
the order (a tool call to Part A) and the guide (a search in knowledge) -- spec 9.1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestType:
    __test__ = False

    code: str
    name: str
    prep_source_id: str


CATALOGUE: dict[str, TestType] = {t.code: t for t in (
    TestType("fbc", "Full blood count", "doc:prep-blood-tests#on-the-day"),
    TestType("fasting-glucose", "Fasting glucose", "doc:prep-blood-tests#fasting-blood-test"),
    TestType("hba1c", "HbA1c", "doc:prep-blood-tests#hba1c"),
    TestType("lipids", "Lipid profile", "doc:prep-blood-tests#cholesterol-lipid-profile"),
    TestType("thyroid", "Thyroid function", "doc:prep-blood-tests#thyroid-function-test"),
    TestType("mri", "MRI scan", "doc:prep-imaging#mri-scan"),
    TestType("mri-contrast", "MRI with contrast", "doc:prep-imaging#mri-with-contrast"),
    TestType("us-abdomen", "Abdominal ultrasound", "doc:prep-imaging#abdominal-ultrasound"),
    TestType("ct", "CT scan", "doc:prep-imaging#ct-scan"),
    TestType("xray", "X-ray", "doc:prep-imaging#x-ray"),
    TestType("colonoscopy", "Colonoscopy",
             "doc:colonoscopy-preparation#preparing-for-a-colonoscopy"),
    TestType("physio", "Physiotherapy referral", "doc:specialties#physiotherapy"),
)}
