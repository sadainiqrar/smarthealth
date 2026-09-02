"""Every model module on disk must be imported by `app/db/all_models.py`.

A module that exists but is never imported leaves its tables off `Base.metadata`,
and Alembic autogenerate then emits a DROP TABLE for them. That is data loss caused
by a forgotten import line, which no ordinary test would notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import app.db.all_models  # noqa: F401  - the import under test

pytestmark = pytest.mark.meta

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULES_DIR = REPO_ROOT / "app" / "modules"


def _model_modules_on_disk() -> set[str]:
    return {
        f"app.modules.{path.parent.name}.models"
        for path in MODULES_DIR.glob("*/models.py")
    }


def test_every_model_module_on_disk_is_imported_by_all_models():
    on_disk = _model_modules_on_disk()
    assert on_disk, "found no model modules — has the layout changed?"

    missing = sorted(name for name in on_disk if name not in sys.modules)
    assert not missing, (
        "these model modules exist but are not imported by app/db/all_models.py:\n  "
        + "\n  ".join(missing)
        + "\nAlembic would emit DROP TABLE for their tables. Add the import."
    )


def test_all_tables_covers_every_module_that_declares_one():
    from app.db.all_models import ALL_TABLES

    assert len(ALL_TABLES) >= 10
    for expected in ("users", "patients", "providers", "appointments", "visits"):
        assert expected in ALL_TABLES
