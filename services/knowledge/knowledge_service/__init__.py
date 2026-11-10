"""The knowledge service (Part B). Its own package, settings, stores and process; it imports
nothing from Part A's `app` and Part A imports nothing from it (tests/meta). It talks to
the rest of the system over Kafka, gRPC and Part A's public HTTP API only (spec D-19).

The generated gRPC bindings live in `services/knowledge/gen` (python scripts/gen_proto.py) and
import themselves as the top-level `smarthealth` package, so that directory goes on the
path here.
"""

import sys
from pathlib import Path

_GEN = str(Path(__file__).resolve().parents[1] / "gen")
if _GEN not in sys.path:
    sys.path.insert(0, _GEN)
