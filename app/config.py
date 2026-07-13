"""Application configuration (pydantic-settings, env-driven).

No secret literal lives in code (Space Instructions §3.4). Secret *material* for
external connections lives in Secrets Manager/SSM; only pointers are stored in
Postgres. The values here are process/runtime config read from the environment.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # --- LLM integration mode (GATE 2 cutover — Architecture §3.4.3) ---------
    # The single runtime gate between the deterministic EchoModel plane and the
    # real Anthropic/OpenAI providers. "mock" (the default) preserves the exact
    # pre-cutover behavior: EchoModel primary + fallback, no external keys, no
    # network. An operator flips this to "live" ONLY after the platform secret is
    # populated and the DPA/no-training terms are confirmed. Read from
    # LUCIEL_INTEGRATION_MODE (ECS injects it; currently "mock").
    integration_mode: str = "mock"

    # JSON blob injected from AWS Secrets Manager (LUCIEL_PLATFORM_SECRET). In
    # "mock" mode this stays the empty-object placeholder "{}" and is never read.
    # In "live" mode it carries the platform LLM keys (ANTHROPIC_API_KEY,
    # OPENAI_API_KEY, ...). Parsed at runtime by ``parse_platform_secret``; the
    # parsed values are NEVER logged (Space Instructions §3.4 / §3.8.3).
    platform_secret: str = "{}"  # noqa: S105 — placeholder JSON, not a secret literal

    # AWS region for the live SecretsManagerStore backend (§3.8.3).
    # Defaults to ca-central-1: residency lock (DEVOPS §2.1, LD 21) — a missing env
    # must never point the secret backend out of region.
    aws_region: str = "ca-central-1"

    # --- LLM model IDs (platform-controlled, §7 #11 — single source of truth) --
    # Centralized here so the cutover changes model selection in ONE place and an
    # admin can never set the model (§7 #11). Founder-confirmed production IDs
    # (first-party Anthropic/OpenAI API, matching the keys in Secrets Manager):
    #   primary  = Claude Sonnet 4.6  ($3/$15 per MTok) — the "competent employee" default
    #   fast     = Claude Haiku 4.5   ($1/$5)  — intra-session no-tools/<4K-ctx routing
    #   fallback = GPT-5.4            ($2.50/$15) — used only on primary failover
    # Override via env (LUCIEL_LLM_*) without a code change.
    llm_anthropic_primary_model: str = "claude-sonnet-4-6"
    llm_anthropic_fast_model: str = "claude-haiku-4-5"
    llm_openai_fallback_model: str = "gpt-5.4"

    # Upper bound on generated tokens per LLM call (platform cost control).
    llm_max_output_tokens: int = 1024


def parse_platform_secret(raw: str) -> dict[str, str]:
    """Parse the LUCIEL_PLATFORM_SECRET JSON blob into a flat str->str mapping.

    The blob is injected from AWS Secrets Manager and holds the platform LLM
    keys (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY). Returns an empty dict for the
    "{}" placeholder or any unparsable/empty input so "mock" mode never faults.

    SECURITY: the parsed values are credentials — this function NEVER logs them.
    On a parse error it logs only the failure fact, not the input (Space
    Instructions §3.4 / Architecture §3.8.3)."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Never log ``raw`` — it may contain secret material.
        logger.warning("LUCIEL_PLATFORM_SECRET is not valid JSON; treating as empty")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("LUCIEL_PLATFORM_SECRET did not decode to an object; treating as empty")
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
