"""Validated runtime configuration. Missing *required* values abort startup in production;
missing *optional* provider credentials surface as NOT_CONFIGURED, never as a mock."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "test", "live-test", "production"]

ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    jarvis_env: Env = "local"
    jarvis_enable_test_doubles: bool = False
    jarvis_log_level: str = "INFO"
    jarvis_worker_id: str | None = None
    jarvis_worker_concurrency: int = Field(3, ge=1, le=8)
    jarvis_task_lease_seconds: int = Field(300, ge=30, le=1800)
    jarvis_encryption_key: SecretStr | None = None            # AES-256-GCM key for stored provider credentials
    jarvis_encryption_key_previous: SecretStr | None = None   # only during key rotation
    # Deployment administrators: may manage provider credentials in the Integrations Center (comma-separated).
    jarvis_admin_user_ids: str = ""
    jarvis_admin_emails: str = ""

    database_url: str = "postgresql://postgres@localhost:5432/postgres"
    database_pool_size: int = Field(5, ge=1, le=20)
    db_health_interval_s: int = Field(30, ge=1, le=300)
    db_reconnect_max_backoff_s: int = Field(60, ge=1, le=600)

    # Auth: Supabase JWT verification. JWKS (asymmetric keys) preferred; HS256 secret supported.
    supabase_url: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwt_secret: SecretStr | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_issuer: str | None = None

    api_public_base_url: str = "http://localhost:8000"
    api_cors_origins: str = "http://localhost:5173"

    # Providers (all optional => NOT_CONFIGURED)
    anthropic_api_key: SecretStr | None = None
    anthropic_admin_key: SecretStr | None = None
    claude_model_fast: str = "claude-haiku-5-5"
    claude_model_build: str = "claude-sonnet-5-5"
    claude_model_escalation: str = "claude-opus-5-5"

    google_places_api_key: SecretStr | None = None
    places_api_base_url: str = "https://places.googleapis.com/v1"
    places_store_coordinates: bool = False
    places_coordinates_ttl_days: int = Field(30, ge=1, le=30)

    serpapi_api_key: SecretStr | None = None
    firecrawl_api_key: SecretStr | None = None
    firecrawl_max_pages_per_prospect: int = Field(5, ge=1, le=20)

    cloudflare_api_token: SecretStr | None = None
    cloudflare_account_id: str | None = None
    cloudflare_pages_preview_project: str = "jarvis-previews"
    preview_ttl_days: int = Field(30, ge=1, le=90)

    email_provider: Literal["resend"] | None = None
    resend_api_key: SecretStr | None = None
    resend_webhook_secret: SecretStr | None = None
    unsubscribe_signing_key: SecretStr | None = None

    livekit_url: str | None = None
    livekit_api_key: SecretStr | None = None
    livekit_api_secret: SecretStr | None = None
    voice_agent_shared_secret: SecretStr | None = None

    # Verification policy
    no_website_min_confidence: float = Field(0.8, ge=0.5, le=1.0)
    broken_second_probe_minutes: int = Field(10, ge=10)
    max_candidate_domains: int = Field(3, ge=1, le=5)
    max_profile_checks: int = Field(3, ge=1, le=5)

    # Sites / QA / sandbox
    sites_artifact_dir: Path = Path("/var/lib/jarvis/sites")
    qa_runner_mode: Literal["runner", "docker", "subprocess"] = "docker"
    sandbox_runner_url: str | None = None
    sandbox_runner_token: SecretStr | None = None
    qa_runner_dir: Path = Path(__file__).resolve().parents[2] / "qa-runner"
    sandbox_image: str = "jarvis-qa-sandbox:latest"
    sandbox_timeout_s: int = Field(300, ge=30, le=1800)

    backup_offsite_configured: bool = False

    @field_validator("claude_model_fast", "claude_model_build", "claude_model_escalation")
    @classmethod
    def _model_allowed(cls, v: str) -> str:
        if v not in ALLOWED_MODELS:
            raise ValueError(f"model {v!r} is not in the allowed list {sorted(ALLOWED_MODELS)}")
        return v

    @model_validator(mode="after")
    def _env_rules(self) -> Settings:
        if self.jarvis_enable_test_doubles and self.jarvis_env != "test":
            raise ValueError("JARVIS_ENABLE_TEST_DOUBLES is only permitted when JARVIS_ENV=test")
        if self.jarvis_env == "production":
            missing = [n for n, v in {
                "JARVIS_ENCRYPTION_KEY": self.jarvis_encryption_key,
                "SUPABASE_URL": self.supabase_url,
            }.items() if not v]
            if not (self.supabase_jwks_url or self.supabase_jwt_secret):
                missing.append("SUPABASE_JWKS_URL or SUPABASE_JWT_SECRET")
            if missing:
                raise ValueError(f"missing required production settings: {', '.join(missing)}")
            if self.qa_runner_mode != "runner":
                raise ValueError("production requires QA_RUNNER_MODE=runner (sandbox runner service; no Docker socket in the worker)")
            if not (self.sandbox_runner_url and self.sandbox_runner_token):
                raise ValueError("production requires SANDBOX_RUNNER_URL and SANDBOX_RUNNER_TOKEN")
            if not self.places_api_base_url.startswith("https://places.googleapis.com/"):
                raise ValueError("PLACES_API_BASE_URL must be https://places.googleapis.com/v1 in production")
        return self

    @property
    def admin_user_ids(self) -> set[str]:
        return {x.strip().lower() for x in self.jarvis_admin_user_ids.split(",") if x.strip()}

    @property
    def admin_emails(self) -> set[str]:
        return {x.strip().lower() for x in self.jarvis_admin_emails.split(",") if x.strip()}

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
