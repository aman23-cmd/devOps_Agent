"""
Centralised configuration loaded from environment variables.

Uses pydantic-settings so every value is validated at startup —
the service will refuse to boot if a required secret is missing,
which is exactly what you want in production.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment variables consumed by the DevOps Agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # ── AI / LLM ─────────────────────────────────────────────
    ANTHROPIC_API_KEY: str
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"

    # ── GitHub ────────────────────────────────────────────────
    GITHUB_TOKEN: str
    GITHUB_WEBHOOK_SECRET: str

    # ── Slack ─────────────────────────────────────────────────
    SLACK_BOT_TOKEN: str
    SLACK_SIGNING_SECRET: str
    SLACK_CHANNEL_ID: str = "#devops-alerts"

    # ── Persistence ───────────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg2://devops:devops@postgres:5432/devops_agent"
    REDIS_URL: str = "redis://redis:6379/0"

    # ── Cloud Providers (optional) ────────────────────────────
    GCP_PROJECT_ID: str | None = None
    AWS_REGION: str | None = None
    AWS_LOG_GROUP: str | None = None

    # ── Application ───────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "production"
    MAX_LOG_LINES: int = 5000  # total cap for truncated CI logs
    REDIS_QUEUE_KEY: str = "pipeline_failures"
    AUTOGEN_MAX_ROUNDS: int = 15

    # ── PagerDuty (optional) ─────────────────────────────────
    PAGERDUTY_ROUTING_KEY: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Singleton accessor — parsed once, cached forever."""
    return Settings()  # type: ignore[call-arg]
