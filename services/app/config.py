import os
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

# Every value below was published in this repository, as a default, an example
# or a documentation placeholder. They are public and are never accepted as a
# secret or password, in any setting.
KNOWN_PUBLIC_VALUES = frozenset(
    {
        "change-me-in-production",
        "generate-a-long-random-string",
        "min-32-chars-secret-change-me-for-production-use",
        "min-32-chars-secret-change-me!!",
        "at-least-32-characters-long-secret-key",
        "0123456789abcdef0123456789abcdef",
        "changeme",
        "authpass",
        "<password>",
        "<api-key>",
        "your-api-key",
    }
)

GENERATE_HINT = (
    "New random values: run tools/generate-env.sh (Linux, macOS) or "
    "tools/generate-env.ps1 (Windows), or use: openssl rand -hex 32"
)

_ENCRYPTION_NOTE = (
    "Changing it makes stored MFA secrets and revealable token and API key values "
    "unreadable; see 'Changing secrets' in README.md."
)


def _is_public(value: str) -> bool:
    return value in KNOWN_PUBLIC_VALUES or value.strip() in KNOWN_PUBLIC_VALUES


def _decoding_stages(value: str) -> list[str]:
    """The value and every further percent-decoding of it, until it stops changing."""
    stages = [value]
    while (decoded := unquote(stages[-1])) != stages[-1]:
        stages.append(decoded)
    return stages


def _postgres_url_problem(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        password = parts.password
    except ValueError:
        # The exception text contains the URL, password included: never pass it on.
        return "DATABASE_URL is not a valid PostgreSQL URL."
    if parts.netloc.count("@") > 1:
        return (
            "DATABASE_URL contains more than one '@' before the host, so the password "
            "cannot be read unambiguously. Write '@' inside the user name or password as %40."
        )
    candidates = parse_qs(parts.query, keep_blank_values=True).get("password", [])
    if password is not None:
        candidates.append(password)
    for candidate in candidates:
        if any(_is_public(stage) for stage in _decoding_stages(candidate)):
            return (
                "DATABASE_URL uses a publicly known password. Change the database user's "
                "password on the PostgreSQL server (ALTER ROLE ... PASSWORD ...), then put the "
                "new password in DATABASE_URL."
            )
    return None


def _redis_url_problem(url: str) -> str | None:
    try:
        password = Redis.from_url(url).get_connection_kwargs().get("password")
    except Exception:
        return "REDIS_URL is not a valid Redis URL (redis://, rediss:// or unix://)."
    if not password:
        return (
            "REDIS_URL has no password. With the bundled Redis, set REDIS_PASSWORD. With an "
            "external Redis, require a password on that server and put it in REDIS_URL."
        )
    if _is_public(password):
        return (
            "REDIS_URL uses a publicly known password. With the bundled Redis, set "
            "REDIS_PASSWORD to a new random value. With an external Redis, change the "
            "password on that server, then update REDIS_URL."
        )
    return None


class Settings(BaseSettings):
    # hide_input_in_errors keeps submitted values, which may be secrets, out of
    # every validation error message.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    port: int = Field(default=8080, validation_alias="PORT")
    database_url: str = Field(default="sqlite:///data/auth.db", validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    admin_api_key: str = Field(validation_alias="ADMIN_API_KEY")
    session_secret: str = Field(validation_alias="SESSION_SECRET")
    app_encryption_key: str = Field(validation_alias="APP_ENCRYPTION_KEY")
    allowed_areas: str = Field(default="orders,billing,webhooks", validation_alias="ALLOWED_AREAS")
    bootstrap_token: str = Field(default="", validation_alias="BOOTSTRAP_TOKEN")
    totp_issuer: str = Field(default="AuthService", validation_alias="TOTP_ISSUER")
    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    validate_cache_ttl_sec: int = Field(default=300, validation_alias="VALIDATE_CACHE_TTL_SEC")
    root_path: str = Field(default="", validation_alias="ROOT_PATH")
    # Must be explicit: True breaks session cookies on plain http://localhost (Secure not sent).
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")
    rate_limit_window_ms: int = Field(default=60_000, validation_alias="RATE_LIMIT_WINDOW_MS")
    rate_limit_max_req: int = Field(default=120, validation_alias="RATE_LIMIT_MAX_REQ")
    activity_log_retention_days: int = Field(default=30, validation_alias="ACTIVITY_LOG_RETENTION_DAYS")
    access_log_retention_days: int = Field(default=30, validation_alias="ACCESS_LOG_RETENTION_DAYS")

    @model_validator(mode="after")
    def _reject_missing_or_public_secrets(self) -> "Settings":
        problems: list[str] = []

        required_secrets = (
            ("ADMIN_API_KEY", self.admin_api_key, "Update every integration that uses the master key."),
            ("SESSION_SECRET", self.session_secret, _ENCRYPTION_NOTE),
            ("APP_ENCRYPTION_KEY", self.app_encryption_key, _ENCRYPTION_NOTE),
        )
        for name, value, note in required_secrets:
            if not value.strip():
                problems.append(f"{name} is empty. Set it to a new random value.")
            elif _is_public(value):
                problems.append(f"{name} uses a publicly known value. Set it to a new random value. {note}")

        if problem := _redis_url_problem(self.redis_url):
            problems.append(problem)

        # The bundled compose file passes REDIS_PASSWORD to this container through
        # env_file; it is the password of the bundled Redis server.
        redis_password = os.environ.get("REDIS_PASSWORD")
        if redis_password and _is_public(redis_password):
            problems.append(
                "REDIS_PASSWORD uses a publicly known value. Set it to a new random value."
            )

        if not self.database_url.startswith("sqlite"):
            if problem := _postgres_url_problem(self.database_url):
                problems.append(problem)
            # asyncpg reads PGPASSWORD from the process environment when the URL has no password.
            pg_password = os.environ.get("PGPASSWORD")
            if pg_password and _is_public(pg_password):
                problems.append(
                    "PGPASSWORD uses a publicly known password. Change the database user's "
                    "password on the PostgreSQL server, then update PGPASSWORD."
                )

        if problems:
            raise PydanticCustomError("insecure_settings", "\n".join(problems))
        return self

    @property
    def allowed_areas_list(self) -> list[str]:
        return [a.strip() for a in self.allowed_areas.split(",") if a.strip()]


def _describe(error: ValidationError) -> str:
    lines: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        name = ".".join(str(part) for part in item["loc"])
        if item["type"] == "missing":
            lines.append(f"{name} is not set.")
        elif item["type"] == "insecure_settings":
            lines.extend(item["msg"].splitlines())
        else:
            lines.append(f"{name}: {item['msg']}")
    body = "\n".join(f"  - {line}" for line in lines)
    return (
        "Configuration error. The service will not start until these are fixed:\n"
        f"{body}\n{GENERATE_HINT}"
    )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        raise SystemExit(_describe(error)) from None
