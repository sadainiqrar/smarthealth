import pytest

from app.core.circuit import CircuitBreaker, CircuitOpen

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_it_opens_after_the_threshold_and_fails_fast():
    clock = _Clock()
    breaker = CircuitBreaker("gw", threshold=3, cooldown_seconds=10, clock=clock)
    for _ in range(2):
        breaker.before_call()
        breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen):
        breaker.before_call()


def test_after_the_cooldown_one_trial_closes_or_reopens_it():
    clock = _Clock()
    breaker = CircuitBreaker("gw", threshold=1, cooldown_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    assert breaker.state == "half-open"
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open", "a failed trial opens it again"
    clock.now = 20
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"


def test_a_success_resets_the_failure_count():
    breaker = CircuitBreaker("gw", threshold=2, clock=_Clock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_an_open_circuit_is_retryable_service_unavailable():
    from app.core.errors import ServiceUnavailable

    assert issubclass(CircuitOpen, ServiceUnavailable)
