"""Remove every demo row. The T3 database is shared by the whole run and other tests count
whole tables, so a demo test must leave nothing behind."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.network import (
    CLINICS,
    LICENCE_PREFIX,
    LINKED_PATIENT_INDEXES,
    MRN_PREFIX,
    PROVIDERS,
    STAFF_LOGINS,
    patient_identity,
)

_PATIENTS = "SELECT id FROM patients WHERE mrn LIKE :mrn"
_PROVIDERS = "SELECT id FROM providers WHERE license_number LIKE :lic"
_DEMO_APPOINTMENT = f"provider_id IN ({_PROVIDERS}) OR patient_id IN ({_PATIENTS})"
_APPOINTMENTS = f"SELECT id FROM appointments WHERE {_DEMO_APPOINTMENT}"


async def purge_demo(session: AsyncSession) -> None:
    params = {"mrn": f"{MRN_PREFIX}%", "lic": f"{LICENCE_PREFIX}%",
              "clinics": [name for name, _ in CLINICS],
              "emails": [e for e, _ in STAFF_LOGINS] + [p.email for p in PROVIDERS]
              + [patient_identity(i).email for i in LINKED_PATIENT_INDEXES]}
    statements = (
        f"DELETE FROM test_orders WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM prescriptions WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM clinical_notes WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM patient_documents WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM notifications WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM billing_records WHERE appointment_id IN ({_APPOINTMENTS})",
        f"DELETE FROM visits WHERE appointment_id IN ({_APPOINTMENTS})",
        f"DELETE FROM waitlist_entries WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM appointments WHERE {_DEMO_APPOINTMENT}",
        f"DELETE FROM provider_slots WHERE provider_id IN ({_PROVIDERS})",
        "DELETE FROM analytics_daily WHERE clinic_id IN "
        "(SELECT id FROM clinics WHERE name = ANY(:clinics))",
        f"DELETE FROM provider_departments WHERE provider_id IN ({_PROVIDERS})",
        "DELETE FROM providers WHERE license_number LIKE :lic",
        "DELETE FROM patients WHERE mrn LIKE :mrn",
        "DELETE FROM departments WHERE clinic_id IN "
        "(SELECT id FROM clinics WHERE name = ANY(:clinics))",
        "DELETE FROM clinics WHERE name = ANY(:clinics)",
        # Exactly the demo's logins, never a pattern: other tests own @example.com users.
        "DELETE FROM users WHERE email = ANY(:emails)",
    )
    for statement in statements:
        await session.execute(text(statement), params)
    await session.commit()
