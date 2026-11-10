"""Puts the Part B services on the import path, as their containers do.

Each service is its own package under services/<name>/ with its own top-level name
(`knowledge_service`, `assistant_service`) and generated bindings; this is the test
suite's equivalent of each Dockerfile's WORKDIR. Importing this module is all a Part B
test needs.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for service in ("knowledge", "assistant"):
    path = str(ROOT / "services" / service)
    if path not in sys.path:
        sys.path.insert(0, path)
