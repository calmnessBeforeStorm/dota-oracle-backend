"""Application settings. Everything configurable lives here, nothing else reads os.environ."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Signs tokens on a laptop and in CI, where nothing is worth protecting and nobody should need
#: setup to run the suite. Production refuses it: it is in a public repository.
DEV_SECRET_KEY = "dev-only-secret-key-refused-in-production"

#: HS256 wants a key at least as long as its 256-bit output.
MIN_SECRET_KEY_BYTES = 32


class Settings(BaseSettings):
    # hide_input_in_errors: Settings carries secrets (SECRET_KEY, STEAM_API_KEY, the DB
    # password) - pydantic's default error rendering embeds the full input value it rejected,
    # which would print those secrets right back into whatever validation failure gets pasted
    # into a log or an issue.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # App. Ports default to the host-side values; docker compose overrides the DB and
    # Redis ones with the in-network 5432/6379.
    # App
    app_env: Literal["local", "development", "production"] = "local"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8100
    cors_origins: str = "http://localhost:5273"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5442
    postgres_db: str = "dota_oracle"
    postgres_user: str = "dota"
    postgres_password: str = "dota"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6389
    redis_db: int = 0

    # External sources (spec section 2)
    opendota_api_key: str | None = None
    stratz_api_token: str | None = None
    #: Whether STRATZ answers from this host at all.
    #:
    #: Not a feature flag - a fact about the network. Measured 2026-09-11 on the production
    #: server: every request came back 403 with a Cloudflare HTML page, with a token verified
    #: identical to one that returned 200 from a residential connection. When false, outcomes
    #: are resolved from OpenDota instead and the STRATZ details backfill is not scheduled.
    stratz_available: bool = True
    steam_api_key: str | None = None
    liquipedia_user_agent: str = "dota-oracle/0.1 (contact@example.com)"

    # Live loop cadence
    live_league_poll_interval: int = Field(default=30, ge=10)
    live_realtime_poll_interval: int = Field(default=15, ge=5)

    # ML
    model_dir: str = "./models"
    active_model_version: str | None = None

    # Auth (design 2026-09-11-auth-and-pipeline-panel, section 1)
    secret_key: str = DEV_SECRET_KEY

    @model_validator(mode="after")
    def production_needs_a_real_secret_key(self) -> Self:
        """Refuse to start rather than sign tokens with a key anybody can read.

        The message never includes the value: a startup failure is exactly what ends up pasted
        into an issue.
        """
        if self.app_env != "production":
            return self
        if self.secret_key == DEV_SECRET_KEY:
            raise ValueError("SECRET_KEY is the development default; set a real one in .env")
        if len(self.secret_key.encode()) < MIN_SECRET_KEY_BYTES:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_SECRET_KEY_BYTES} bytes in production; "
                "generate one with: openssl rand -base64 48"
            )
        return self

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations synchronously."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
