import subprocess
import sys

import pytest
from pydantic import BaseModel, ValidationError

from app.core.pagination import MAX_LIMIT, Page, PageParams

pytestmark = pytest.mark.unit


class _Item(BaseModel):
    name: str


def test_page_params_hold_limit_and_offset():
    params = PageParams(limit=25, offset=50)
    assert params.limit == 25
    assert params.offset == 50


def test_a_page_reports_the_total_not_just_the_slice():
    """Without a total a client cannot tell whether more rows exist."""
    page = Page[_Item](
        items=[_Item(name="a")], total=137, limit=50, offset=100
    )
    assert page.total == 137
    assert len(page.items) == 1
    assert page.offset == 100


def test_the_maximum_limit_is_bounded():
    """A single request must not be able to ask for every row in the table."""
    assert MAX_LIMIT == 200


def test_a_page_rejects_a_negative_total():
    with pytest.raises(ValidationError):
        Page[_Item](items=[], total=-1, limit=50, offset=0)


def test_importing_pagination_does_not_load_fastapi_or_starlette():
    """Must run in a fresh interpreter: a pytest process that already imported FastAPI
    for another test would make this pass regardless of what `app.core.pagination`
    itself imports, defeating the point of the check.

    A Temporal activity, a Celery task, or any plain script must be able to build a
    `PageParams` or return a `Page` without pulling in the ASGI stack — that is why the
    `page_params` FastAPI dependency lives in `app.api.deps`, not here.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.core.pagination; "
            "print('fastapi' in sys.modules, 'starlette' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False"
