"""coherence constraints

Composite foreign keys tying appointments' provider/clinic to their slot's
provider/clinic, and departments' clinic to their slot's/appointment's clinic.
Every existing FK, CHECK, the GiST exclusion constraint, and the partial unique
index on appointments were individually satisfied while an appointment could
still claim a slot belonging to a different provider or a different clinic, and
a slot's department could belong to a different clinic than the slot itself.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Composite unique targets (must exist before the FKs referencing them) ---

    # Composite target for the department/clinic coherence FKs.
    op.create_unique_constraint(
        "uq_departments_id_clinic", "departments", ["id", "clinic_id"]
    )

    # Composite target for appointments' coherence FK: lets an appointment prove
    # its slot really belongs to the provider and clinic it claims.
    op.create_unique_constraint(
        "uq_provider_slots_id_provider_clinic",
        "provider_slots",
        ["id", "provider_id", "clinic_id"],
    )

    # --- Composite foreign keys ---

    # A slot's department must belong to the slot's own clinic.
    op.create_foreign_key(
        "fk_provider_slots_department_clinic",
        "provider_slots",
        "departments",
        ["department_id", "clinic_id"],
        ["id", "clinic_id"],
    )

    # The slot must belong to the provider and clinic this appointment names.
    # MATCH SIMPLE means this is skipped while slot_id is NULL (pre-claim) and
    # enforced the moment a slot is attached.
    op.create_foreign_key(
        "fk_appointments_slot_provider_clinic",
        "appointments",
        "provider_slots",
        ["slot_id", "provider_id", "clinic_id"],
        ["id", "provider_id", "clinic_id"],
    )

    # The department must belong to the clinic this appointment names.
    op.create_foreign_key(
        "fk_appointments_department_clinic",
        "appointments",
        "departments",
        ["department_id", "clinic_id"],
        ["id", "clinic_id"],
    )

    # A confirmed appointment without a claimed slot is a contradiction: confirmed
    # is only reachable after slot reservation succeeds. `op.create_check_constraint`
    # re-applies the target metadata's naming convention to the name it's given (the
    # same way the ORM model's `name="confirmed_requires_slot"` gets expanded), so
    # passing the bare name here - not the already-expanded one - is what keeps the
    # migration and the model producing the identical `alembic check`-clean name:
    # `ck_appointments_confirmed_requires_slot`.
    op.create_check_constraint(
        "confirmed_requires_slot",
        "appointments",
        "status <> 'confirmed' OR slot_id IS NOT NULL",
    )


def downgrade() -> None:
    # Same naming-convention re-application applies to op.drop_constraint as to
    # op.create_check_constraint above: pass the bare name, not the expanded one.
    op.drop_constraint("confirmed_requires_slot", "appointments", type_="check")
    op.drop_constraint(
        "fk_appointments_department_clinic", "appointments", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_appointments_slot_provider_clinic", "appointments", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_provider_slots_department_clinic", "provider_slots", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_provider_slots_id_provider_clinic", "provider_slots", type_="unique"
    )
    op.drop_constraint("uq_departments_id_clinic", "departments", type_="unique")
