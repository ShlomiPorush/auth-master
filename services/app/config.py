from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = Field(default=8080, validation_alias="PORT")
    database_url: str = Field(default="sqlite:///data/auth.db", validation_alias="DATABASE_URL")
    redis_url: str = Field(default="redis://:changeme@127.0.0.1:6379", validation_alias="REDIS_URL")
    admin_api_key: str = Field(default="change-me-in-production", validation_alias="ADMIN_API_KEY")
    session_secret: str = Field(
        default="min-32-chars-secret-change-me-for-production-use",
        validation_alias="SESSION_SECRET",
    )
    app_encryption_key: str = Field(default="0123456789abcdef0123456789abcdef", validation_alias="APP_ENCRYPTION_KEY")
    allowed_areas: str = Field(default="orders,billing,webhooks", validation_alias="ALLOWED_AREAS")
    bootstrap_token: str = Field(default="", validation_alias="BOOTSTRAP_TOKEN")
    totp_issuer: str = Field(default="AuthService", validation_alias="TOTP_ISSUER")
    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    validate_cache_ttl_sec: int = Field(default=300, validation_alias="VALIDATE_CACHE_TTL_SEC")
    root_path: str = Field(default="", validation_alias="ROOT_PATH")
    # Must be explicit: True breaks session cookies on plain http://localhost (Secure not sent).
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")

    @property
    def allowed_areas_list(self) -> list[str]:
        return [a.strip() for a in self.allowed_areas.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
