import pytest

from app import worker

pytestmark = pytest.mark.contract


def test_check_exits_nonzero_when_temporal_is_unreachable(monkeypatch):
    """Lives in T1, not T0: it dials a closed port to prove the real exception path exits 1."""
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_HOST", "127.0.0.1")
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_PORT", "1")
    assert worker.main(["--check"]) == 1
