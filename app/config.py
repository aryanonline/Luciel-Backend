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

    # --- Stripe integration posture (Architecture §3.4.1b) ---------------------
    # Mirrors the LLM GATE 2 cutover shape ("integration_mode") one level down: a
    # dedicated posture for the Stripe seam so Stripe can move to a real TEST-mode
    # binding independently of the LLM cutover. Values: "mock" (default —
    # FakeStripeGateway, no keys, no network) | "test" (RealStripeGateway bound
    # against Stripe TEST keys — sk_test_/pk_test_/whsec_ — read from the platform
    # secret, never hardcoded). "live" is DELIBERATELY NOT a supported value here:
    # this codebase never binds a production Stripe secret key (sk_live_) — that
    # cutover is out of scope / owner-gated and not implemented by this seam.
    stripe_integration_posture: str = "mock"

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
    # Platform sender identity for outbound notification email (verify-email,
    # escalation, dunning, export-ready) via SES (§3.1.2/§3.6.7). Must be a
    # SES-verified identity. Empty in mock/CI -> NullEmailSender (audit-only).
    platform_email_sender: str = ""
    # Public base URL used to build customer-facing links in emails (verify link,
    # export download). Falls back to a dev localhost for local runs.
    app_base_url: str = "http://127.0.0.1:3000"
    # hCaptcha signup bot-protection (§3.7.1a). Empty -> verification is a no-op
    # (mock/CI/local); set in live mode to enforce siteverify on signup.
    hcaptcha_secret_key: str = ""
    hcaptcha_site_key: str = ""
    # Non-production ONLY: when True, the signup captcha check accepts the fixed
    # seed/live-test bypass token so the §10 harness can create synthetic accounts
    # through the real API in a live-configured dev/preprod. MUST remain False in prod
    # (prod never sets it) — default False is the fail-safe. See app/auth/captcha.py.
    allow_captcha_bypass_token: bool = False
    # Twilio SMS/voice send (§3.1.4). REST auth needs the Account SID (AC...) plus an
    # API key SID (SK...) + secret. Empty -> NullSmsSender (mock/CI/unconfigured).
    # NOTE: live SMS is blocked until the Account SID (AC...) is provided.
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    # Meta channels (WhatsApp, Instagram DM, Messenger — §3.1.2). App secret verifies
    # inbound X-Hub-Signature-256; access token authorizes Graph API sends. Empty ->
    # NullMetaSender + inbound signature check can't pass (mock/CI/unconfigured).
    # Per-tenant page/WA tokens live on the channel_auth connection in full deploy;
    # this platform-level token covers the shared Graph app.
    meta_app_secret: str = ""
    meta_access_token: str = ""
    # Meta webhook GET-challenge verify token (owner-chosen string, set in the Meta
    # app webhook config and matched on hub.verify_token).
    meta_verify_token: str = ""
    # Twilio ACCOUNT Auth Token (32-hex) — required for INBOUND X-Twilio-Signature
    # verification (distinct from the API-Key used for sending). Empty -> inbound
    # signature verification cannot pass in live mode (documented dependency).
    twilio_auth_token: str = ""

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

    # --- Knowledge-sync connector OAuth (Architecture §3.2.3 / §3.8.3) ---------
    # Client id/secret + redirect URI per OAuth-class knowledge-sync provider
    # (Google Drive, HubSpot, Salesforce). These are the APPLICATION's OAuth-client
    # registration values (who the app claims to be to the provider) — NOT the
    # per-tenant access/refresh token, which is real secret material and lives in
    # the SecretStore (§3.8.3), never here. Still never hardcoded: sourced from env
    # (LUCIEL_ prefix) which in production is injected from Secrets Manager, exactly
    # like ``platform_secret`` above. Defaults are empty so mock-mode/CI never needs
    # them and a live OAuth flow fails closed (no authorize URL) rather than using a
    # placeholder client id.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""  # noqa: S105 — empty default, not a secret literal
    google_drive_redirect_uri: str = ""
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""  # noqa: S105
    hubspot_redirect_uri: str = ""
    salesforce_consumer_key: str = ""
    salesforce_consumer_secret: str = ""  # noqa: S105
    salesforce_redirect_uri: str = ""

    # --- Website crawl (§3.2.3 crawl-sync note) ---------------------------------
    # No OAuth — just an httpx fetch. Bounded so a misconfigured crawl target can
    # never hang a sync worker or pull in an unbounded page.
    website_crawl_timeout_seconds: float = 10.0
    website_crawl_max_bytes: int = 5_000_000

    def model_post_init(self, __context: object) -> None:
        """Overlay platform-owned OAuth app credentials from the platform-secret
        blob onto the (empty) config fields (§3.8.3 platform-owned class).

        Per Architecture §3.8.3, platform-owned API keys are delivered via Secrets
        Manager. The OAuth *app* client secrets are the same class, so in live mode
        they arrive inside ``LUCIEL_PLATFORM_SECRET`` (the injected blob) rather than
        as separate env vars. A direct env var (LUCIEL_GOOGLE_DRIVE_CLIENT_ID, ...)
        still wins when set (local/CI); the blob only fills a field left empty. The
        per-tenant OAuth *tokens* are NOT touched here — those are real secret
        material stored as tenant-scoped secret_ref ARNs by the connections layer.
        """
        blob = parse_platform_secret(self.platform_secret)
        if not blob:
            return
        # blob key -> settings attribute. Only platform-owned APP credentials.
        # NOTE: the dict VALUES are Pydantic field NAMES (not secret values); bandit's
        # B105 heuristic flags the name strings ending in secret/token/key, hence the
        # per-line `# nosec B105` on those entries. No secret material is hardcoded here.
        overlay = {
            "GOOGLE_DRIVE_CLIENT_ID": "google_drive_client_id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "google_drive_client_secret",  # nosec B105
            "GOOGLE_DRIVE_REDIRECT_URI": "google_drive_redirect_uri",
            "HUBSPOT_CLIENT_ID": "hubspot_client_id",
            "HUBSPOT_CLIENT_SECRET": "hubspot_client_secret",  # nosec B105
            "HUBSPOT_REDIRECT_URI": "hubspot_redirect_uri",
            "SALESFORCE_CONSUMER_KEY": "salesforce_consumer_key",
            "SALESFORCE_CONSUMER_SECRET": "salesforce_consumer_secret",  # nosec B105
            "SALESFORCE_REDIRECT_URI": "salesforce_redirect_uri",
            "PLATFORM_EMAIL_SENDER": "platform_email_sender",
            "HCAPTCHA_SECRET_KEY": "hcaptcha_secret_key",  # nosec B105
            "HCAPTCHA_SITE_KEY": "hcaptcha_site_key",
            "TWILIO_ACCOUNT_SID": "twilio_account_sid",
            "TWILIO_API_KEY_SID": "twilio_api_key_sid",
            "TWILIO_API_KEY_SECRET": "twilio_api_key_secret",  # nosec B105
            "TWILIO_CLIENT_SECRET": "twilio_api_key_secret",  # nosec B105
            "META_APP_SECRET": "meta_app_secret",  # nosec B105
            "META_ACCESS_TOKEN": "meta_access_token",  # nosec B105
            "META_VERIFY_TOKEN": "meta_verify_token",  # nosec B105
            "TWILIO_AUTH_TOKEN": "twilio_auth_token",  # nosec B105
        }
        # app_base_url has a non-empty localhost DEFAULT, so the `not getattr` guard
        # below would skip it. Bridge it explicitly: the platform secret's APP_BASE_URL
        # (the public frontend origin used in customer-facing links, e.g. the verify
        # email link) must override the localhost default in deployed environments.
        if blob.get("APP_BASE_URL"):
            object.__setattr__(self, "app_base_url", blob["APP_BASE_URL"])

        # JWT signing key: security-critical. Bridge it from the platform secret; it has
        # a KNOWN dev-default (published in source), so in live mode we must FAIL CLOSED
        # if it wasn't overridden — a forgeable-session vuln must never reach a real env.
        if blob.get("JWT_SIGNING_KEY"):
            object.__setattr__(self, "jwt_signing_key", blob["JWT_SIGNING_KEY"])
        if (
            self.integration_mode == "live"
            and self.jwt_signing_key == "dev-only-not-a-secret-rotate-in-prod"
        ):
            raise RuntimeError(
                "JWT_SIGNING_KEY is still the insecure dev default in live mode — refusing "
                "to start (would make all session tokens forgeable). Set JWT_SIGNING_KEY "
                "in the platform secret."
            )

        # CORS allowed origins (dashboard_origins) also default to localhost, so the
        # deployed frontend origin must be added or the browser blocks every API call
        # from the real site (login/signup fetch fails silently at the CORS preflight).
        # DASHBOARD_ORIGINS in the secret is a comma-separated list; we also always fold
        # in the app_base_url origin so the public frontend can call the API.
        origins: list[str] = []
        raw_origins = blob.get("DASHBOARD_ORIGINS")
        if raw_origins:
            origins.extend(o.strip() for o in str(raw_origins).split(",") if o.strip())
        if blob.get("APP_BASE_URL"):
            origins.append(str(blob["APP_BASE_URL"]).rstrip("/"))

        # Fold in the www<->apex counterpart of every https origin. CORS origin matching
        # is exact, so allowing https://vantagemind.ai does NOT allow
        # https://www.vantagemind.ai (or vice versa). Users landing on the www host had
        # every API call blocked at the preflight (signup/login 'Something went wrong').
        # Generating both variants makes the canonical + www hosts both work regardless
        # of which one the browser is on.
        def _with_www_variants(items: list[str]) -> list[str]:
            out: list[str] = []
            for o in items:
                out.append(o)
                if o.startswith("https://"):
                    host = o[len("https://") :]
                    if host.startswith("www."):
                        out.append("https://" + host[len("www.") :])
                    else:
                        out.append("https://www." + host)
            return out

        origins = _with_www_variants(origins)
        if origins:
            # De-dupe while preserving order; keep localhost defaults for local dev too.
            merged = list(dict.fromkeys([*origins, *self.dashboard_origins]))
            object.__setattr__(self, "dashboard_origins", merged)
        for blob_key, attr in overlay.items():
            if not getattr(self, attr, "") and blob.get(blob_key):
                object.__setattr__(self, attr, blob[blob_key])

        # Bool bridge (non-production seam): the platform secret may carry the
        # captcha-bypass flag as a string; coerce it. Absent -> stays False (fail-safe).
        bypass_raw = blob.get("ALLOW_CAPTCHA_BYPASS_TOKEN")
        if bypass_raw is not None and not self.allow_captcha_bypass_token:
            object.__setattr__(
                self,
                "allow_captcha_bypass_token",
                str(bypass_raw).strip().lower() in {"1", "true", "yes", "on"},
            )


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
