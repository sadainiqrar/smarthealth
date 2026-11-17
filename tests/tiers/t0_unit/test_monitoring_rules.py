"""Numbers to plain statuses (system health spec §4.3). Pure, so every boundary is a
one-line test."""

import pytest

from app.modules.monitoring import rules
from app.modules.monitoring.rules import Verdict

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("age", "status"), [(5, "ok"), (59.9, "ok"), (60, "bad"), (None, "bad")])
def test_heartbeat(age, status):
    assert rules.heartbeat(age).status == status


def test_a_missing_heartbeat_says_not_reporting():
    assert rules.heartbeat(None) == Verdict("bad", "not reporting")


@pytest.mark.parametrize(
    ("total", "failed", "status"),
    [
        (0, 0, "idle"),
        (4, 4, "ok"),      # under five runs: too few to judge
        (5, 0, "ok"),
        (100, 4, "ok"),
        (100, 5, "warn"),  # at or above 5 %
        (100, 20, "bad"),  # at or above 20 %
        (None, None, "unknown"),
    ],
)
def test_failure_share(total, failed, status):
    assert rules.failure_share(total, failed).status == status


def test_too_few_runs_still_names_the_failures():
    assert rules.failure_share(3, 1).reason == "1 of 3 failed (too few to judge)"


@pytest.mark.parametrize(
    ("key", "value", "status"),
    [
        ("outbox_age_s", 59, "ok"), ("outbox_age_s", 60, "warn"), ("outbox_age_s", 300, "bad"),
        ("consumer_lag", 100, "warn"), ("queue_ready", 1000, "bad"),
        ("dead_letters", 0, "ok"), ("dead_letters", 1, "warn"), ("dead_letters", 10, "bad"),
        ("first_token_p95_s", 4.9, "ok"), ("first_token_p95_s", 15, "bad"),
        ("retrieval_no_results_pct", 25, "warn"),
        ("outbox_age_s", None, "unknown"),
    ],
)
def test_levels(key, value, status):
    assert rules.level(key, value).status == status


def test_worst_of_picks_the_most_serious():
    assert rules.worst(["ok", "idle", "warn", "ok"]) == "warn"
    assert rules.worst(["ok", "bad", "unknown"]) == "bad"
    assert rules.worst([]) == "unknown"
