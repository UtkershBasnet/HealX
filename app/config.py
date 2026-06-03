"""
HealX Configuration — Type-safe settings loaded from environment variables.

Uses pydantic-settings for validation and .env file support.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── GitHub ───
    github_token: str = ""
    github_webhook_secret: str = ""

    # ─── LLM ───
    api_key: str = ""

    # ─── Database ───
    database_url: str = "postgresql+asyncpg://healx:healx@localhost:5432/healx"

    # ─── Redis ───
    redis_url: str = "redis://localhost:6379/0"

    # ─── Observability ───
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ─── App ───
    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def sync_database_url(self) -> str:
        """Return a synchronous database URL for Alembic migrations."""
        return self.database_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://"
        )


settings = Settings()
