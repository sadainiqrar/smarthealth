"""Reading a database constraint name off a failed write.

Framework-free by the same rule as `app.core.errors` and `app.core.audit`: SQLAlchemy
only, no FastAPI. Week 2's Temporal activities translate `IntegrityError` into domain
errors on the same code path the HTTP layer uses, so this must import cleanly in a
process that has never heard of an ASGI app.

Lives in `app.db` rather than in any one module because every service that writes a
uniquely-constrained row needs the identical lookup, and the lookup is not obvious.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def violated_constraint(exc: IntegrityError) -> str | None:
    """The name of the constraint or unique index that rejected the write.

    `exc.orig` is *not* the asyncpg error: SQLAlchemy's asyncpg dialect re-raises it as
    its own `IntegrityError` shim, which carries only `sqlstate`/`pgcode`. The original
    `asyncpg.exceptions.UniqueViolationError` -- the object holding `constraint_name` --
    hangs off it as `__cause__`. Verified against the running database; matching on
    `exc.orig` alone always missed, which turned a duplicate MRN into a 500.
    """
    for candidate in (exc.orig.__cause__ if exc.orig is not None else None, exc.orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
