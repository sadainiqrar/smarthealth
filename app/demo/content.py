"""What imported clinical history says: cases per specialty, plain data.

Every specialty of the knowledge corpus (`services/knowledge/corpus/specialties.md`) has
cases, so whatever specialty the assistant names, a patient has been seen there. Test codes
come from `app/modules/clinical/catalogue.py`, so an imported order carries the same
preparation guidance a live one does. Medications are ordinary, widely used ones; this is
demonstration content, not clinical advice.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Medication:
    medication: str
    dose: str
    frequency: str
    duration_days: int
    instructions: str = ""


@dataclass(frozen=True)
class Case:
    reason: str  # what the patient booked for, as the front desk wrote it
    complaint: str
    findings: str
    summary: str  # the patient-facing summary
    diagnoses: tuple[tuple[str, str], ...]  # (description, ICD-10 code)
    medications: tuple[Medication, ...] = ()
    tests: tuple[str, ...] = ()  # codes from the test catalogue


SPECIALTIES: tuple[str, ...] = (
    "Cardiology", "Dermatology", "Endocrinology", "Gastroenterology", "General Practice",
    "Neurology", "Orthopaedics", "Paediatrics", "Physiotherapy",
    "Ear, Nose and Throat (ENT)",
)

CONTENT: dict[str, tuple[Case, ...]] = {
    "Cardiology": (
        Case("Blood pressure review", "Headaches and high home blood-pressure readings.",
             "BP 152/94 seated, repeated 148/92. Heart sounds normal.",
             "Your blood pressure is higher than it should be. We started a daily tablet "
             "and asked for a blood test; please keep a home BP diary.",
             (("Essential hypertension", "I10"),),
             (Medication("Amlodipine", "5 mg", "once daily", 90, "Take in the morning."),),
             ("lipids",)),
        Case("Chest tightness on exertion", "Tightness climbing stairs, settles with rest.",
             "ECG sinus rhythm, no acute changes. BMI 29.",
             "Your heart tracing was normal today. We ordered cholesterol tests and a scan "
             "to look further; call the emergency number if pain comes at rest.",
             (("Stable angina, under investigation", "I20.8"),),
             (Medication("Atorvastatin", "20 mg", "once daily", 180, "Take in the evening."),),
             ("lipids", "ct")),
    ),
    "Dermatology": (
        Case("Recurring itchy rash", "Itchy dry patches on elbows and hands for months.",
             "Erythematous scaly plaques, flexural, no infection.",
             "This is eczema. Use the cream twice a day for two weeks and moisturise daily.",
             (("Atopic dermatitis", "L20.9"),),
             (Medication("Hydrocortisone 1% cream", "thin layer", "twice daily", 14,
                         "Apply to affected skin only."),)),
        Case("Mole check", "Would like a mole on the back checked.",
             "6 mm symmetrical, evenly pigmented naevus; dermoscopy benign.",
             "The mole looks harmless. We photographed it to compare in twelve months — come "
             "back sooner if it changes in size, shape or colour, or bleeds.",
             (("Melanocytic naevus", "D22.5"),)),
    ),
    "Endocrinology": (
        Case("Thyroid review", "Tiredness, weight gain and feeling cold.",
             "Dry skin, pulse 58, no goitre.",
             "Your symptoms may be from an underactive thyroid. We started a tablet and will "
             "recheck your thyroid blood test.",
             (("Hypothyroidism", "E03.9"),),
             (Medication("Levothyroxine", "50 mcg", "once daily", 180,
                         "Take on an empty stomach, 30 minutes before breakfast."),),
             ("thyroid",)),
        Case("Diabetes review", "Thirst and passing more urine.",
             "Random glucose 13.1 mmol/L, BMI 31.",
             "Your sugar levels are high, consistent with type 2 diabetes. We started "
             "metformin and booked blood tests.",
             (("Type 2 diabetes mellitus", "E11.9"),),
             (Medication("Metformin", "500 mg", "twice daily", 180, "Take with meals."),),
             ("hba1c", "fasting-glucose")),
    ),
    "Gastroenterology": (
        Case("Heartburn", "Burning after meals and at night for six weeks.",
             "Epigastric tenderness, no alarm features.",
             "This is acid reflux. Take the tablet before breakfast and avoid late meals.",
             (("Gastro-oesophageal reflux disease", "K21.9"),),
             (Medication("Omeprazole", "20 mg", "once daily", 56,
                         "Take 30 minutes before breakfast."),)),
        Case("Change in bowel habit", "Loose stools and cramps for two months.",
             "Soft abdomen, mild lower tenderness.",
             "We have arranged a colonoscopy to look at the bowel. Follow the preparation "
             "guide carefully.",
             (("Altered bowel habit, under investigation", "R19.4"),), (),
             ("colonoscopy", "fbc")),
    ),
    "General Practice": (
        Case("Cough", "Cough and sore throat for five days.",
             "Temperature 37.6, chest clear, throat red.",
             "This is a viral infection and should settle within two weeks. Rest and drink "
             "fluids; return if you are short of breath.",
             (("Acute upper respiratory infection", "J06.9"),),
             (Medication("Paracetamol", "1 g", "every 6 hours as needed", 5,
                         "No more than 4 g in 24 hours."),)),
        Case("Annual check-up", "Routine check, feels well.",
             "BP 128/80, BMI 26, examination normal.",
             "Everything looked healthy today. We ordered routine blood tests.",
             (("General health examination", "Z00.0"),), (),
             ("fbc", "lipids")),
    ),
    "Neurology": (
        Case("Migraines", "Throbbing one-sided headaches with nausea, twice a month.",
             "Neurological examination normal.",
             "These are migraines. Take the tablet at the start of an attack and keep a "
             "headache diary.",
             (("Migraine without aura", "G43.0"),),
             (Medication("Sumatriptan", "50 mg", "at onset, may repeat after 2 hours", 90,
                         "Take at the start of a migraine; check the leaflet for the "
                         "maximum in 24 hours."),)),
        Case("Numbness in hand", "Tingling in the thumb and fingers at night.",
             "Positive Phalen's test, normal power.",
             "This is likely carpal tunnel syndrome. Wear a wrist splint at night; we "
             "ordered a scan.",
             (("Carpal tunnel syndrome", "G56.0"),), (), ("mri",)),
    ),
    "Orthopaedics": (
        Case("Knee pain", "Knee pain on stairs and after running.",
             "Mild effusion, medial joint-line tenderness.",
             "Your knee shows wear in the cartilage. An X-ray and physiotherapy will help.",
             (("Osteoarthritis of the knee", "M17.9"),),
             (Medication("Ibuprofen", "400 mg", "three times daily with food", 10,
                         "Stop if you get stomach pain."),),
             ("xray", "physio")),
        Case("Lower back pain", "Back pain after lifting, no leg symptoms.",
             "Paraspinal spasm, straight-leg raise negative.",
             "This is a back strain. Keep moving gently; it usually settles in a few weeks.",
             (("Low back strain", "S39.012"),), (), ("physio",)),
    ),
    "Paediatrics": (
        Case("Ear pain", "Ear pain and fever since yesterday.",
             "Red bulging left eardrum, temperature 38.2.",
             "Your child has a middle-ear infection. Give the antibiotic for the full course "
             "and paracetamol for pain.",
             (("Acute otitis media", "H66.9"),),
             (Medication("Amoxicillin oral suspension", "by weight, set by the prescriber",
                         "three times daily", 5, "Shake well; finish the course."),)),
        Case("Growth check", "Routine growth and development review.",
             "Height and weight on the 50th centile, development normal.",
             "Your child is growing and developing well.",
             (("Routine child health examination", "Z00.1"),)),
    ),
    "Physiotherapy": (
        Case("Shoulder rehabilitation", "Stiff right shoulder after a fall six weeks ago.",
             "Reduced abduction to 120 degrees, strength 4/5.",
             "We started an exercise programme. Do the exercises daily and come back in two "
             "weeks.",
             (("Shoulder stiffness after injury", "M25.611"),)),
        Case("Back rehabilitation", "Ongoing lower back stiffness.",
             "Reduced lumbar flexion, core weakness.",
             "We set you core-strength exercises. Little and often works best.",
             (("Chronic low back pain", "M54.5"),)),
    ),
    "Ear, Nose and Throat (ENT)": (
        Case("Blocked nose", "Blocked nose and post-nasal drip for months.",
             "Swollen nasal turbinates, clear discharge.",
             "This is rhinitis. Use the nasal spray daily for at least a month.",
             (("Chronic rhinitis", "J31.0"),),
             (Medication("Mometasone nasal spray", "2 sprays each nostril", "once daily", 30,
                         "Aim away from the middle of the nose."),)),
        Case("Sore throats", "Recurrent sore throats this year.",
             "Enlarged tonsils, no current infection.",
             "We will keep a record of throat infections; come back if they continue.",
             (("Recurrent tonsillitis", "J03.91"),)),
    ),
}

CANCELLATION_REASONS: tuple[str, ...] = (
    "Patient called to cancel", "Feeling better", "Work commitment",
    "Booked elsewhere", "Transport problem", "Illness in the family",
)

#: The text of an imported result document, per test code (stored as its extracted text).
RESULT_TEXT: dict[str, str] = {
    "fbc": "Full blood count: haemoglobin, white cells and platelets within normal limits.",
    "fasting-glucose": "Fasting glucose: 7.4 mmol/L (above the normal range).",
    "hba1c": "HbA1c: 58 mmol/mol (7.5%).",
    "lipids": "Lipid profile: total cholesterol 6.1 mmol/L, LDL 3.9 mmol/L.",
    "thyroid": "Thyroid function: TSH 7.8 mU/L (raised), free T4 low-normal.",
    "mri": "MRI: no structural abnormality; findings consistent with the clinical picture.",
    "ct": "CT: no acute abnormality.",
    "xray": "X-ray: mild joint-space narrowing, no fracture.",
    "colonoscopy": "Colonoscopy: no polyps or inflammation seen to the caecum.",
    "physio": "Physiotherapy assessment completed; programme of six sessions agreed.",
}
