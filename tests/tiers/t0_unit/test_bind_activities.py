import pytest

from app.core.temporal import bind_activities

pytestmark = pytest.mark.unit


class Owner:
    async def act(self) -> str:
        return "bound"


async def module_level() -> str:
    return "free"


async def test_methods_bind_to_their_owner_and_functions_pass_through():
    owner = Owner()
    bound = bind_activities([Owner.act, module_level], [owner])
    assert bound[0].__self__ is owner
    assert bound[1] is module_level


def test_a_method_with_no_instance_fails_loudly():
    with pytest.raises(LookupError, match="Owner"):
        bind_activities([Owner.act], [])
