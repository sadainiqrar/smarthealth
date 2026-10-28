import pytest

from app import worker

pytestmark = pytest.mark.unit


def test_no_arguments_means_run():
    assert worker.parse_args([]).check is False


def test_check_flag_selects_the_probe():
    assert worker.parse_args(["--check"]).check is True


def test_check_exits_nonzero_when_temporal_is_unreachable(monkeypatch):
    """The compose healthcheck relies on the exit code, so a dead server must fail it."""
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_HOST", "127.0.0.1")
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_PORT", "1")
    assert worker.main(["--check"]) == 1
