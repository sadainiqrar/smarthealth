import asyncio
from datetime import timedelta

import pytest

from app import worker

pytestmark = pytest.mark.unit


def test_no_arguments_means_run():
    assert worker.parse_args([]).check is False


def test_check_flag_selects_the_probe():
    assert worker.parse_args(["--check"]).check is True


def test_check_exits_nonzero_when_connect_hangs(monkeypatch):
    """A blackholed host makes the SDK connect for tens of seconds; the probe must give
    up on its own, inside compose's healthcheck timeout."""

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker, "connect_client", hang)
    monkeypatch.setattr(worker, "CHECK_TIMEOUT", timedelta(seconds=0.2))
    assert worker.main(["--check"]) == 1
