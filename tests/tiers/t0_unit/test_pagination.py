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
