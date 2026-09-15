import pytest

from app.settings import Settings, get_settings

pytestmark = pytest.mark.unit


def test_names_are_unprefixed_by_default():
    settings = Settings(resource_prefix="")
    assert settings.topic("appointments.booked") == "appointments.booked"
    assert settings.queue("notifications") == "notifications"
    assert settings.task_queue("booking") == "booking"


def test_prefix_is_applied_to_every_resource_name():
    settings = Settings(resource_prefix="t_ab12_")
    assert settings.topic("appointments.booked") == "t_ab12_appointments.booked"
    assert settings.queue("notifications") == "t_ab12_notifications"
    assert settings.task_queue("booking") == "t_ab12_booking"


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("SMARTHEALTH_RESOURCE_PREFIX", "t_zz99_")
    monkeypatch.setenv("SMARTHEALTH_ENVIRONMENT", "test")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.resource_prefix == "t_zz99_"
    assert settings.environment == "test"
    get_settings.cache_clear()


def test_llm_mode_defaults_to_fixture():
    assert Settings().llm_mode == "fixture"


def test_log_level_defaults_to_info_and_reads_the_environment(monkeypatch):
    """Verbosity has to be changeable without a redeploy.

    `configure_logging` was called with no argument for the whole of Week 1, which
    pinned the root logger to INFO in code — so raising verbosity to debug a live
    incident meant editing a source file.
    """
    assert Settings().log_level == "INFO"

    monkeypatch.setenv("SMARTHEALTH_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    try:
        assert get_settings().log_level == "DEBUG"
    finally:
        get_settings.cache_clear()
