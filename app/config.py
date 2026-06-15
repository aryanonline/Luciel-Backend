"""Application configuration (pydantic-settings, env-driven).

No secret literal lives in code (Space Instructions §3.4). Secret *material* for
external connections lives in Secrets Manager/SSM; only pointers are stored in
Postgres. The values here are process/runtime config read from the environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUCIEL_", env_file=".env", extra="ignore")

    # --- Database ---------------------------------------------------------
    # Async SQLAlchemy URL. App connects as the NON-superuser app role so RLS applies.
    database_url: str = "postgresql+asyncpg://luciel_app:luciel_dev_pw@127.0.0.1:5432/luciel_dev"
    # Sync URL for Alembic (psycopg v3 driver).
    database_url_sync: str = (
        "postgresql+psycopg://luciel_app:luciel_dev_pw@127.0.0.1:5432/luciel_dev"
    )

    # --- Redis ------------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- Session / auth ---------------------------------------------------
    # JWT signing key. In production this is sourced from Secrets Manager and injected
    # as an env var; the default here is a dev-only placeholder, never a real secret.
    jwt_signing_key: str = "dev-only-not-a-secret-rotate-in-prod"  # noqa: S105
    jwt_algorithm: str = "HS256"
    session_ttl_seconds: int = 60 * 60 * 12  # short-lived session token
    session_cookie_name: str = "luciel_session"
    # Secure flag is True in prod; allow override to False for local http dev.
    cookie_secure: bool = True
    cookie_samesite: str = "lax"

    # --- CORS (dashboard origin runs on its own origin; credentials:'include') ---
    dashboard_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # --- Link TTLs (Architecture §3.7.1a) --------------------------------
    verification_link_ttl_seconds: int = 24 * 60 * 60  # 24h
    reset_link_ttl_seconds: int = 60 * 60  # 1h
    unverified_reap_after_seconds: int = 7 * 24 * 60 * 60  # 7 days
    resend_limit_count: int = 3
    resend_limit_window_seconds: int = 15 * 60  # 3 per 15 min

    # --- Billing (Architecture §7 #15) -----------------------------------
    free_allowance: int = 50
    payg_block_size: int = 100
    payg_price_per_block_cents: int = 3900  # $39.00 per 100-block

    # --- Grounding (Architecture §9 #18) ---------------------------------
    grounding_floor: float = 0.50

    # --- Lifecycle (Architecture §3.6) -----------------------------------
    grace_window_days: int = 30
    export_link_ttl_days: int = 7

    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
