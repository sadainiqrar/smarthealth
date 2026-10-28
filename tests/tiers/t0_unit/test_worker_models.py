"""The worker process must load every model, not just the ones its imports reach.

In the test process some other module has always imported every model, so a worker that
forgot to is invisible there. The first activity to run in the real container then dies
resolving a foreign key to a table nobody registered. A fresh interpreter is the only
honest place to ask.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

PROBE = """
import app.worker
from app.db.base import Base
for table in Base.metadata.tables.values():
    for fk in table.foreign_keys:
        fk.column  # raises NoReferencedTableError if the target table is unregistered
"""


def test_a_fresh_worker_process_resolves_every_foreign_key():
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-800:]
