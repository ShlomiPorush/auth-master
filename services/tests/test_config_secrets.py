"""The service refuses to start with a missing or publicly known secret.

Every secret used here is generated per run with ``secrets``. Requires pytest.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.config import KNOWN_PUBLIC_VALUES, Settings, get_settings  # noqa: E402

# The uncached loader, so these tests never touch the settings that other test
# modules cached when they imported the app.
load_settings = get_settings.__wrapped__

ENCRYPTION_AND_KEY_SETTINGS = ("ADMIN_API_KEY", "SESSION_SECRET", "APP_ENCRYPTION_KEY")
CLEARED = (
    *ENCRYPTION_AND_KEY_SETTINGS,
    "REDIS_URL",
    "REDIS_PASSWORD",
    "DATABASE_URL",
    "PGPASSWORD",
)


def fresh() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A valid configuration with per-run values; .env files are not read."""
    monkeypatch.chdir(tmp_path)  # no .env in the working directory
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    values = {
        "ADMIN_API_KEY": fresh(),
        "SESSION_SECRET": fresh(),
        "APP_ENCRYPTION_KEY": fresh(),
        "REDIS_URL": f"redis://:{fresh()}@127.0.0.1:6379",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    yield values


def startup_error() -> str:
    with pytest.raises(SystemExit) as exc:
        load_settings()
    return str(exc.value.code)


def test_valid_configuration_loads(env):
    s = load_settings()
    assert s.admin_api_key == env["ADMIN_API_KEY"]
    assert s.database_url.startswith("sqlite")


@pytest.mark.parametrize("name", [*ENCRYPTION_AND_KEY_SETTINGS, "REDIS_URL"])
def test_missing_secret_is_named(env, monkeypatch, name):
    monkeypatch.delenv(name)
    message = startup_error()
    assert f"{name} is not set." in message
    assert "Configuration error" in message


@pytest.mark.parametrize("name", ENCRYPTION_AND_KEY_SETTINGS)
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_secret_is_refused(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert f"{name} is empty" in startup_error()


@pytest.mark.parametrize("name", ENCRYPTION_AND_KEY_SETTINGS)
@pytest.mark.parametrize("value", sorted(KNOWN_PUBLIC_VALUES))
def test_every_public_value_is_refused_in_every_secret(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    message = startup_error()
    assert f"{name} uses a publicly known value" in message


def test_public_value_with_surrounding_space_is_refused(env, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", " 0123456789abcdef0123456789abcdef ")
    assert "APP_ENCRYPTION_KEY uses a publicly known value" in startup_error()


def test_encryption_secret_message_explains_the_impact(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "min-32-chars-secret-change-me!!")
    assert "unreadable" in startup_error()


def test_all_problems_are_reported_together(env, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "change-me-in-production")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("REDIS_URL", "redis://:changeme@redis:6379")
    message = startup_error()
    for name in ("ADMIN_API_KEY", "APP_ENCRYPTION_KEY", "REDIS_URL"):
        assert name in message


# ── Redis ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "redis://:changeme@redis:6379",
        "redis://default:changeme@redis:6379/0",
        "redis://:%63hangeme@redis:6379",
        "redis://redis:6379?password=changeme",
        "redis://redis:6379/0?password=%63hangeme",
        "rediss://:changeme@redis:6380",
        "unix:///run/redis.sock?password=changeme",
        "redis://:authpass@redis:6379",
    ],
)
def test_public_redis_password_is_refused(env, monkeypatch, url):
    monkeypatch.setenv("REDIS_URL", url)
    assert "REDIS_URL uses a publicly known password" in startup_error()


@pytest.mark.parametrize("url", ["redis://redis:6379", "redis://redis:6379/0", "redis://:@redis:6379"])
def test_redis_url_without_password_is_refused(env, monkeypatch, url):
    monkeypatch.setenv("REDIS_URL", url)
    assert "REDIS_URL has no password" in startup_error()


@pytest.mark.parametrize("url", ["http://redis:6379", "not a url"])
def test_invalid_redis_url_is_refused(env, monkeypatch, url):
    monkeypatch.setenv("REDIS_URL", url)
    assert "REDIS_URL is not a valid Redis URL" in startup_error()


@pytest.mark.parametrize(
    "template",
    [
        "rediss://:{p}@redis:6380",
        "redis://app:{p}@redis:6379/1",
        "unix:///run/redis.sock?password={p}",
        "redis://redis:6379?password={p}",
    ],
)
def test_private_redis_password_is_accepted(env, monkeypatch, template):
    monkeypatch.setenv("REDIS_URL", template.format(p=fresh()))
    load_settings()


def test_public_redis_password_variable_is_refused(env, monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "changeme")
    assert "REDIS_PASSWORD uses a publicly known value" in startup_error()


def test_private_redis_password_variable_is_accepted(env, monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", fresh())
    load_settings()


# ── PostgreSQL ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "postgres://auth:authpass@postgres:5432/authdb",
        "postgresql://auth:authpass@postgres/authdb",
        "postgres://auth:%61uthpass@postgres/authdb",
        "postgres://auth:%2561uthpass@postgres/authdb",
        "postgres://auth@postgres/authdb?password=authpass",
        "postgres://auth@postgres/authdb?sslmode=require&password=%2561uthpass",
        "postgres://auth:<password>@postgres:5432/authdb",
        "postgres://auth:changeme@postgres/authdb",
    ],
)
def test_public_postgres_password_is_refused(env, monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    assert "DATABASE_URL uses a publicly known password" in startup_error()


def test_unparsable_postgres_url_does_not_leak_the_password(env, monkeypatch):
    # A full-width solidus normalizes to '/', so urlsplit raises ValueError with the netloc in it.
    password = fresh()
    monkeypatch.setenv("DATABASE_URL", f"postgres://auth:{password}@db／host/authdb")
    message = startup_error()
    assert "DATABASE_URL is not a valid PostgreSQL URL." in message
    assert password not in message


def test_ambiguous_postgres_url_is_refused(env, monkeypatch):
    # urllib splits at the last '@', asyncpg at the first: refuse instead of guessing.
    monkeypatch.setenv("DATABASE_URL", "postgres://auth:authpass@%2Frun%2Fpostgresql@primary/authdb")
    assert "more than one '@'" in startup_error()


@pytest.mark.parametrize(
    "template",
    [
        "postgres://auth:{p}@postgres:5432/authdb",
        "postgres://auth@postgres/authdb?password={p}",
        "postgres://auth@postgres/authdb",  # password from PGPASSWORD, .pgpass or a certificate
    ],
)
def test_private_or_absent_postgres_password_is_accepted(env, monkeypatch, template):
    monkeypatch.setenv("DATABASE_URL", template.format(p=fresh()))
    load_settings()


def test_public_pgpassword_is_refused(env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://auth@postgres/authdb")
    monkeypatch.setenv("PGPASSWORD", "authpass")
    assert "PGPASSWORD uses a publicly known password" in startup_error()


def test_pgpassword_is_not_checked_for_sqlite(env, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "authpass")
    load_settings()


# ── Error output ───────────────────────────────────────────────


def test_error_output_never_contains_submitted_values(env, monkeypatch):
    marker = fresh()
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("REDIS_URL", f"redis://user{marker}@redis:6379")
    monkeypatch.setenv("PORT", f"not-a-number-{marker}")
    message = startup_error()
    assert marker not in message
    assert "PORT" in message


def test_validation_error_hides_input(env, monkeypatch):
    marker = fresh()
    monkeypatch.setenv("ADMIN_API_KEY", "your-api-key")
    monkeypatch.setenv("PORT", marker)
    with pytest.raises(Exception) as exc:
        Settings(_env_file=None)
    assert marker not in str(exc.value)
    assert "your-api-key" not in str(exc.value)


def test_message_points_to_the_generator(env, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY")
    assert "tools/generate-env.sh" in startup_error()
    assert config.GENERATE_HINT in startup_error()
