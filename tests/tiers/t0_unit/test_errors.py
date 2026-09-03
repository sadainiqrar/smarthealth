import subprocess
import sys

import pytest

from app.core.errors import Conflict, DomainError, NotFound, PermissionDenied

pytestmark = pytest.mark.unit


def test_each_error_carries_its_http_status():
    assert NotFound("x").status_code == 404
    assert Conflict("x").status_code == 409
    assert PermissionDenied("x").status_code == 403


def test_the_detail_is_preserved():
    error = NotFound("patient 123 does not exist")
    assert error.detail == "patient 123 does not exist"
    assert str(error) == "patient 123 does not exist"


def test_every_domain_error_subclasses_the_base():
    """Starlette resolves handlers by walking the MRO, so one handler on the base
    class catches all of them — but only if they actually inherit from it."""
    for error_type in (NotFound, Conflict, PermissionDenied):
        assert issubclass(error_type, DomainError)


def test_the_base_class_is_not_raised_directly():
    """A bare DomainError has no meaningful status; it exists to be subclassed."""
    assert DomainError.status_code == 500


def test_importing_errors_does_not_load_fastapi_or_starlette():
    """Must run in a fresh interpreter: a pytest process that already imported FastAPI
    for another test would make this pass regardless of what `app.core.errors` itself
    imports, defeating the point of the check.

    A Temporal activity, a Celery task, or any plain script must be able to import
    `DomainError` and its subclasses without pulling in the ASGI stack — that is the
    entire reason this module was split from `app.api.error_handlers`.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.core.errors; "
            "print('fastapi' in sys.modules, 'starlette' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False"
