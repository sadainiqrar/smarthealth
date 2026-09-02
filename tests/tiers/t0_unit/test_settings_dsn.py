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


def test_credentials_with_reserved_characters_are_url_encoded():
    """An unencoded '@' or '/' in a password silently corrupts the DSN's authority."""
    settings = Settings(postgres_user="a/b", postgres_password="p@ss word")
    assert "a%2Fb:p%40ss+word@" in settings.postgres_dsn


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
