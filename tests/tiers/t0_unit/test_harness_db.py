import pytest

from tests.harness.db import alembic_config, quote_identifier

pytestmark = pytest.mark.unit


def test_identifier_quoting_wraps_in_double_quotes():
    assert quote_identifier("t_ab12_gw0_db") == '"t_ab12_gw0_db"'


def test_identifier_quoting_escapes_embedded_quotes():
    """CREATE DATABASE cannot be parameterised, so the identifier must be escaped."""
    assert quote_identifier('we"ird') == '"we""ird"'


def test_identifier_quoting_rejects_a_null_byte():
    with pytest.raises(ValueError, match="null byte"):
        quote_identifier("bad\x00name")


def test_alembic_config_points_at_the_repo_migrations():
    config = alembic_config()
    assert config.get_main_option("script_location").endswith("migrations")
