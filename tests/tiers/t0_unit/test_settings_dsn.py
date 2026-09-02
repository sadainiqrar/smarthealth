import pytest

from app.settings import Settings

pytestmark = pytest.mark.unit


def test_postgres_dsn_is_built_for_asyncpg():
    settings = Settings(
        postgres_host="db.internal", postgres_port=15432,
        postgres_user="smarthealth", postgres_password="secret", postgres_db="sh_test",
    )
    assert settings.postgres_dsn == (
        "postgresql+asyncpg://smarthealth:secret@db.internal:15432/sh_test"
    )


def test_postgres_admin_url_targets_the_maintenance_database():
    """Creating or dropping a per-run database cannot be done while connected to it."""
    settings = Settings(
        postgres_host="db.internal", postgres_port=15432,
        postgres_user="smarthealth", postgres_password="secret", postgres_db="sh_test",
    )
    assert settings.postgres_admin_url == (
        "postgresql://smarthealth:secret@db.internal:15432/postgres"
    )


def test_credentials_round_trip_through_a_url_parser():
    """Assert on what a parser reads back, not on an encoded substring.

    The previous version of this test asserted the literal text `p%40ss+word`, which
    codified a bug: `quote_plus` encodes a space as `+`, and a URL parser reads that
    back as a literal plus, silently yielding a different password.
    """
    from sqlalchemy.engine import make_url

    for password in ("p@ss word", "p/ss", "p:ss", "p#ss", "p?ss", "pa+ss", "pässword"):
        settings = Settings(postgres_user="a/b", postgres_password=password)
        url = make_url(settings.postgres_dsn)
        assert url.username == "a/b", f"username corrupted for password {password!r}"
        assert url.password == password, f"password corrupted: {url.password!r}"


def test_a_space_in_a_password_survives_the_round_trip():
    """The specific regression: quote_plus turned a space into '+'."""
    from sqlalchemy.engine import make_url

    settings = Settings(postgres_password="two words")
    assert "+" not in make_url(settings.postgres_dsn).password
    assert make_url(settings.postgres_dsn).password == "two words"


def test_an_ipv6_host_is_bracketed():
    """An unbracketed IPv6 literal makes the authority unparseable."""
    from sqlalchemy.engine import make_url

    settings = Settings(postgres_host="::1", postgres_port=15432)
    url = make_url(settings.postgres_dsn)
    assert url.host in ("::1", "[::1]")
    assert url.port == 15432


def test_an_ipv6_host_is_bracketed_in_mongo_and_redis_urls():
    settings = Settings(mongo_host="::1", mongo_port=27018, redis_host="::1", redis_port=16379)
    assert settings.mongo_uri == "mongodb://[::1]:27018"
    assert settings.redis_url == "redis://[::1]:16379/0"


def test_ordinary_hosts_are_not_bracketed():
    settings = Settings(postgres_host="db.internal", mongo_host="mongo.internal",
                        redis_host="redis.internal")
    assert "[" not in settings.postgres_dsn
    assert settings.mongo_uri == "mongodb://mongo.internal:27017"
    assert settings.redis_url == "redis://redis.internal:6379/0"


def test_mongo_and_redis_urls():
    settings = Settings(
        mongo_host="mongo.internal", mongo_port=27018, mongo_db="sh_audit",
        redis_host="redis.internal", redis_port=16379, redis_db=3,
    )
    assert settings.mongo_uri == "mongodb://mongo.internal:27018"
    assert settings.mongo_db == "sh_audit"
    assert settings.redis_url == "redis://redis.internal:16379/3"


def test_isolation_env_names_match_settings_fields(monkeypatch):
    """The harness's isolation layer sets these exact variables; a rename breaks isolation."""
    from tests.harness.isolation import make_isolation

    isolation = make_isolation(run_id="ab12cd34", worker_id="gw0")
    for key, value in isolation.as_env().items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.resource_prefix == isolation.resource_prefix
    assert settings.postgres_db == isolation.postgres_db
    assert settings.mongo_db == isolation.mongo_db
    assert settings.redis_db == isolation.redis_db


def test_jwt_defaults_are_present():
    settings = Settings()
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_expiry_minutes > 0
