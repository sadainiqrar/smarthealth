"""Generate corpus/colonoscopy-preparation.pdf, the corpus's PDF document.

Committed alongside its output so the PDF can be regenerated, not because the index needs
this script: the ingestion pipeline reads the PDF like any uploaded one. Requires fpdf2
(a development tool only).

    python services/knowledge/scripts/make_sample_pdf.py
"""

from pathlib import Path

from fpdf import FPDF

OUT = Path(__file__).resolve().parents[1] / "corpus" / "colonoscopy-preparation.pdf"

SECTIONS = [
    ("Colonoscopy preparation",
     "A colonoscopy examines the inside of the large bowel with a camera. The bowel must be "
     "completely empty for the camera to see clearly, so the preparation starts several days "
     "before your appointment."),
    ("Seven days before",
     "Stop iron tablets. If you take blood thinners such as warfarin, apixaban or clopidogrel, "
     "or medicines for diabetes, ask your clinician whether to change them; do not stop them "
     "yourself."),
    ("Two days before",
     "Eat a low-fibre diet: white bread, white rice, pasta, chicken, fish, eggs and potatoes "
     "without skin. Avoid fruit, vegetables, nuts, seeds and wholegrain foods."),
    ("The day before",
     "Have a light breakfast, then clear fluids only: water, clear soup, black tea or coffee, "
     "and clear apple juice. Take the bowel preparation laxative exactly as instructed on the "
     "packet, usually in two doses, and stay near a toilet."),
    ("On the day",
     "Continue clear fluids until two hours before the procedure, then nothing by mouth. You "
     "may be offered sedation; if so, someone must take you home and stay with you for twelve "
     "hours, and you must not drive until the next day."),
]


def main() -> None:
    pdf = FPDF()
    pdf.add_page()
    for index, (heading, text) in enumerate(SECTIONS):
        pdf.set_font("Helvetica", "B", 16 if index == 0 else 13)
        pdf.multi_cell(0, 9, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    pdf.output(str(OUT))
    print(OUT)


if __name__ == "__main__":
    main()
