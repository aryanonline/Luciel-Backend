"""Application configuration (pydantic-settings, env-driven).

No secret literal lives in code (Space Instructions §3.4). Secret *material* for
external connections lives in Secrets Manager/SSM; only pointers are stored in
Postgres. The values here are process/runtime config read from the environment.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The deterministic signing secret the in-process Stripe fake verifies against. It lives
# here (not only in app.billing.stripe_gateway) because live-mode startup must be able to
# REFUSE it: it is published in this repo, so anything it verifies is forgeable. Defined
# in config to keep the fail-closed check free of a billing import cycle.
FAKE_STRIPE_WEBHOOK_SECRET = "whsec_fake_test_only"  # noqa: S105  # nosec B105


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
    # Round 5B item 17 — dual-key rotation window: set to the OLD key while
    # rotating so existing sessions/links/OAuth states verify until their own
    # TTLs expire; signing always uses the current key. Unset to close the
    # window. Empty = no window open.
    jwt_signing_key_previous: str = ""  # noqa: S105 — empty placeholder, not a secret
    jwt_algorithm: str = "HS256"
    session_ttl_seconds: int = 60 * 60 * 12  # short-lived session token
    session_cookie_name: str = "luciel_session"
    # Secure flag is True in prod; allow override to False for local http dev.
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    # Parent domain for the session cookie, e.g. ".vantagemind.ai". REQUIRED when the
    # frontend and API live on different hosts of the same site (www.vantagemind.ai vs
    # api.vantagemind.ai): without it the cookie is host-only to the API host, so the
    # frontend host never receives it and the Next middleware bounces every logged-in
    # user back to /login. Empty => host-only cookie (correct for localhost dev).
    cookie_domain: str = ""

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

    # --- Pending free-allowance reduction (Legal §A5) -------------------------
    # A REDUCTION to the free starter allowance owes every existing account 30 days'
    # notice delivered BOTH by email and in-dashboard on next login (the in-dashboard
    # half exists because a registered address may have gone stale). The platform
    # operator announces a pending reduction by setting these two; empty/0 means no
    # change is pending, which is why every account's notices[] is empty by default.
    # An INCREASE needs no notice, so it is never announced here.
    free_allowance_reduction_effective_at: str = ""  # ISO-8601 instant
    free_allowance_reduction_to: int = 0  # the new, lower allowance

    # --- Published in-dashboard notices (Legal §A5 / §A10 / §B6) --------------------
    # The two fields above let the platform DERIVE the allowance-reduction notice, whose
    # copy it can write itself from the numbers. They are not a general notice source:
    # §A10's price change and §B6's sub-processor addition also owe an advance
    # in-dashboard notice, and their copy is operator-authored, so nothing could publish
    # them. This is that source — a JSON ARRAY of notice objects:
    #   [{"noticeId": "...", "kind": "price_change", "title": "...", "body": "...",
    #     "effectiveAt": "2026-09-01T00:00:00Z", "learnMoreHref": "/billing"}]
    # ``kind`` must be one of allowance_reduction | price_change | terms_change, and
    # ``noticeId`` must be STABLE for a given announcement (it is the key the dashboard
    # dismisses on). Malformed entries are dropped with a warning rather than breaking
    # every login. Empty (the default) means no notice is published, so notices[] is
    # empty — the normal state. Also readable from the platform secret's
    # PLATFORM_NOTICES so an announcement can be published without a redeploy.
    platform_notices: str = ""

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

    # Stripe webhook signing secret (whsec_...) — the ONLY thing standing between the
    # billing webhook and a forged event, so it is platform-owned secret material like
    # jwt_signing_key: delivered in the platform secret, never in source. Empty default
    # means "no configured secret", which keeps mock/CI on the deterministic fake
    # constant; live mode fails closed instead (see ``_apply_platform_secret``).
    stripe_webhook_secret: str = ""  # noqa: S105 — empty placeholder, not a secret

    # The Stripe metered PRICE the PAYG subscription item is bound to (``price_...``).
    # Usage can only be reported against a subscription item, and an item only exists
    # against a price, so with this empty the gateway returns no subscription and
    # reporting stays SILENT rather than billing at a guessed price (§3.4.1b). It is an
    # account-specific Stripe object id, never a constant, so it can never be hardcoded
    # here: the operator supplies it per environment via LUCIEL_STRIPE_PAYG_PRICE_ID or
    # the platform secret's STRIPE_PAYG_PRICE_ID. Empty is correct for mock mode, where
    # nothing is billed at all.
    stripe_payg_price_id: str = ""

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
    # Bucket the account-export ZIPs land in (§3.6.6). Settable because one hardcoded
    # name cannot exist in two environments: the CDK stack names it per-env and injects
    # the value, so dev exports cannot be written into the prod bucket (P1-14).
    export_bucket: str = "vm-export-bundles"
    # Transcript cold-storage bucket (Commitment #38, §3.4.10): the 90-day hot→cold
    # offload's home. Same per-env naming rule as export_bucket — CDK injects the
    # real name (LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET); this default is the local fallback.
    transcript_archive_bucket: str = "vm-transcript-archive"
    # Voice reply audio bucket (§3.1.2 <Play> hosting): where ElevenLabs-synthesized
    # turn audio lives for the seconds Twilio needs to fetch it. Same per-env naming
    # rule — CDK injects LUCIEL_VOICE_AUDIO_BUCKET; the presence of that env var (not
    # this default) is what binds the real store, mirroring transcript_archive_bucket.
    voice_audio_bucket: str = "vm-voice-audio"
    # How long a voice-audio presigned link stays valid. A call turn needs it for
    # seconds; 15 minutes covers Twilio's retries with margin. The BUCKET lifecycle
    # (1 day) is the storage bound; this is only the link's life.
    voice_audio_link_ttl_seconds: int = 900
    # Platform sender identity for outbound notification email (verify-email,
    # escalation, dunning, export-ready) via SES (§3.1.2/§3.6.7). Must be a
    # SES-verified identity. Empty in mock/CI -> NullEmailSender (audit-only).
    platform_email_sender: str = ""
    # Round 5B item 13 — SES configuration set routing bounce/complaint events to
    # SNS, and the topic the events webhook pins its subscriptions to. Empty =
    # the pipeline is off (mock/CI); deployed envs set both via the platform
    # secret / task env (SES_CONFIGURATION_SET / SES_EVENTS_TOPIC_ARN).
    ses_configuration_set: str = ""
    ses_events_topic_arn: str = ""
    # TTL for escalation-contact confirmation links (round 5B item 13).
    escalation_contact_link_ttl_seconds: int = 7 * 24 * 3600
    # Public base URL used to build customer-facing links in emails (verify link,
    # export download). Falls back to a dev localhost for local runs.
    app_base_url: str = "http://127.0.0.1:3000"
    # The PUBLIC API origin (https://api.vantagemind.ai in dev/prod) — what external
    # providers must be pointed at (Twilio webhook auto-config, c20). Distinct from
    # app_base_url (the FRONTEND origin used in customer-facing links). Empty default
    # = auto-config disabled (the attach falls back to manual webhook guidance);
    # deployed environments set it via the platform secret's API_BASE_URL.
    public_api_base_url: str = ""
    # Dashboard page the provider's OAuth callback bounces the browser back to, relative
    # to app_base_url. Settable because the redirect URIs registered with the providers
    # are frozen but the frontend's route is not. Must name a route the frontend actually
    # serves: connections live on the Configure page, and pointing this at a non-existent
    # /dashboard/connections 404'd the admin immediately after they granted consent.
    oauth_return_path: str = "/dashboard/configure"
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
    # Meta channels (WhatsApp, Instagram DM, Messenger — §3.1.2). The App secret is a
    # property of the shared Graph APP and stays platform-level: it verifies the inbound
    # X-Hub-Signature-256 on webhooks Meta delivers to one URL for every tenant. Empty ->
    # the inbound signature check cannot pass (mock/CI/unconfigured).
    #
    # There is deliberately no platform ACCESS token here. Sending authority is per-tenant
    # (P1-1) and comes off the tenant's channel_auth connection secret_ref; a platform-wide
    # token would make one env var the sender for every tenant on the box.
    meta_app_secret: str = ""
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

    # Knowledge embedding model (§3.2.2). Native 1536 dims matches the
    # `vector(1536)` column, so the model can be swapped without a reindex migration.
    knowledge_embedding_model: str = "text-embedding-3-small"

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
    # ONE Google Cloud OAuth client serves every Google product (Drive knowledge sync
    # AND Calendar): a Google client is not per-API — the same client_id requests
    # whichever scopes the authorize URL asks for. Only the SCOPE differs per product,
    # and that lives in connections.registry.
    google_client_id: str = ""
    google_client_secret: str = ""  # noqa: S105 — empty default, not a secret literal
    google_redirect_uri: str = ""
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""  # noqa: S105
    hubspot_redirect_uri: str = ""
    salesforce_consumer_key: str = ""
    salesforce_consumer_secret: str = ""  # noqa: S105
    salesforce_redirect_uri: str = ""
    calendly_client_id: str = ""
    calendly_client_secret: str = ""  # noqa: S105
    calendly_redirect_uri: str = ""
    notion_client_id: str = ""
    notion_client_secret: str = ""  # noqa: S105
    notion_redirect_uri: str = ""
    # Meta OAuth app (Decision #7). The app SECRET already arrives in the platform blob
    # for inbound X-Hub-Signature verification; the app ID and redirect complete the
    # authorization-code flow for the WhatsApp / Instagram channel connect.
    meta_app_id: str = ""
    meta_redirect_uri: str = ""
    # Instagram is a SEPARATE Meta app (the "Instagram API with Business Login for
    # Instagram" product), with its own app id/secret and its own OAuth host — the
    # Facebook dialog rejects the instagram_* scopes outright. Empty -> the Instagram
    # connection reports configured=false and offers no authorize URL, exactly like an
    # unprovisioned Calendly; it is not a boot failure. See connections.registry.
    instagram_app_id: str = ""
    instagram_app_secret: str = ""  # noqa: S105
    instagram_redirect_uri: str = ""
    # Microsoft Entra app for the BYO Outlook mailbox (§3.1.6a; owner decision
    # 2026-08-10). One multi-tenant + personal-accounts registration serves every
    # customer; /common in the registry endpoints is what makes that work. Empty ->
    # the Outlook option reports configured=false and offers no authorize URL, same
    # honest posture as an unprovisioned Instagram app; never a boot failure.
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""  # noqa: S105
    microsoft_redirect_uri: str = ""
    # Twilio OAuth app (audit round 3, C10 — the one-click SMS/voice connect).
    # DISTINCT from the twilio_* API-key fields above: those are the PLATFORM's
    # own account credentials (fallback sender/probe + inbound signature token);
    # these identify the platform's Twilio OAuth *application* for the
    # authorization-code flow that binds a CUSTOMER's account. Note the legacy
    # blob alias TWILIO_CLIENT_SECRET maps to twilio_api_key_secret — the OAuth
    # keys below are deliberately TWILIO_OAUTH_* so the two can never collide.
    # Empty -> the connect flow falls back to the credential form (Twilio stays
    # available: the customer's own API keys need no platform OAuth app).
    twilio_oauth_client_id: str = ""
    twilio_oauth_client_secret: str = ""  # noqa: S105
    twilio_oauth_redirect_uri: str = ""

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
        # NB: an EMPTY blob must NOT short-circuit this method. The fail-closed checks
        # below (JWT signing key, Stripe webhook secret) are exactly the checks a
        # live deploy with an unprovisioned secret needs, and an early `return` here
        # meant `platform_secret="{}"` — or whitespace, or unparsable JSON — sailed
        # past both of them and booted live with a forgeable session key. Every
        # overlay below is already written as `blob.get(...)`, so an empty mapping is
        # a no-op for them; only the guards change behaviour.
        blob = parse_platform_secret(self.platform_secret, live=self.integration_mode == "live")
        # blob key -> settings attribute. Only platform-owned APP credentials.
        # NOTE: the dict VALUES are Pydantic field NAMES (not secret values); bandit's
        # B105 heuristic flags the name strings ending in secret/token/key, hence the
        # per-line `# nosec B105` on those entries. No secret material is hardcoded here.
        overlay = {
            # The Google app credential is one client for Drive + Calendar. GOOGLE_DRIVE_*
            # is the key set the blob was first provisioned under and names that same
            # client, so it is still accepted; GOOGLE_* wins when both are present.
            "GOOGLE_CLIENT_ID": "google_client_id",
            "GOOGLE_DRIVE_CLIENT_ID": "google_client_id",
            "GOOGLE_CLIENT_SECRET": "google_client_secret",  # nosec B105
            "GOOGLE_DRIVE_CLIENT_SECRET": "google_client_secret",  # nosec B105
            "GOOGLE_REDIRECT_URI": "google_redirect_uri",
            "GOOGLE_DRIVE_REDIRECT_URI": "google_redirect_uri",
            "HUBSPOT_CLIENT_ID": "hubspot_client_id",
            "HUBSPOT_CLIENT_SECRET": "hubspot_client_secret",  # nosec B105
            "HUBSPOT_REDIRECT_URI": "hubspot_redirect_uri",
            "SALESFORCE_CONSUMER_KEY": "salesforce_consumer_key",
            "SALESFORCE_CONSUMER_SECRET": "salesforce_consumer_secret",  # nosec B105
            "SALESFORCE_REDIRECT_URI": "salesforce_redirect_uri",
            "CALENDLY_CLIENT_ID": "calendly_client_id",
            "CALENDLY_CLIENT_SECRET": "calendly_client_secret",  # nosec B105
            "CALENDLY_REDIRECT_URI": "calendly_redirect_uri",
            "NOTION_CLIENT_ID": "notion_client_id",
            "NOTION_CLIENT_SECRET": "notion_client_secret",  # nosec B105
            "NOTION_REDIRECT_URI": "notion_redirect_uri",
            "META_APP_ID": "meta_app_id",
            "META_REDIRECT_URI": "meta_redirect_uri",
            "INSTAGRAM_APP_ID": "instagram_app_id",
            "INSTAGRAM_REDIRECT_URI": "instagram_redirect_uri",
            "MICROSOFT_CLIENT_ID": "microsoft_client_id",
            "MICROSOFT_REDIRECT_URI": "microsoft_redirect_uri",
            "MICROSOFT_CLIENT_SECRET": "microsoft_client_secret",  # nosec B105
            "PLATFORM_EMAIL_SENDER": "platform_email_sender",
            # Not secret material, but it is per-environment Stripe account state that
            # travels with the Stripe keys — so it is rotated in the same blob rather
            # than pinned in a task definition.
            "STRIPE_PAYG_PRICE_ID": "stripe_payg_price_id",
            # Lets an operator PUBLISH a §A5/§A10/§B6 notice by rotating the secret,
            # rather than needing a task-definition change to reach every account.
            "PLATFORM_NOTICES": "platform_notices",
            "HCAPTCHA_SECRET_KEY": "hcaptcha_secret_key",  # nosec B105
            "HCAPTCHA_SITE_KEY": "hcaptcha_site_key",
            "TWILIO_ACCOUNT_SID": "twilio_account_sid",
            "TWILIO_API_KEY_SID": "twilio_api_key_sid",
            "TWILIO_API_KEY_SECRET": "twilio_api_key_secret",  # nosec B105
            "TWILIO_CLIENT_SECRET": "twilio_api_key_secret",  # nosec B105
            "TWILIO_OAUTH_CLIENT_ID": "twilio_oauth_client_id",
            "TWILIO_OAUTH_CLIENT_SECRET": "twilio_oauth_client_secret",  # nosec B105
            "TWILIO_OAUTH_REDIRECT_URI": "twilio_oauth_redirect_uri",
            "META_APP_SECRET": "meta_app_secret",  # nosec B105
            "META_VERIFY_TOKEN": "meta_verify_token",  # nosec B105
            "INSTAGRAM_APP_SECRET": "instagram_app_secret",  # nosec B105
            "TWILIO_AUTH_TOKEN": "twilio_auth_token",  # nosec B105
            # Round 5B item 13: the SES events pipeline (bounce/complaint) —
            # also settable as plain task env (the CDK wires both ways).
            "SES_CONFIGURATION_SET": "ses_configuration_set",
            "SES_EVENTS_TOPIC_ARN": "ses_events_topic_arn",
        }
        # app_base_url has a non-empty localhost DEFAULT, so the `not getattr` guard
        # below would skip it. Bridge it explicitly: the platform secret's APP_BASE_URL
        # (the public frontend origin used in customer-facing links, e.g. the verify
        # email link) must override the localhost default in deployed environments.
        if blob.get("APP_BASE_URL"):
            object.__setattr__(self, "app_base_url", blob["APP_BASE_URL"])
        # The public API origin is bridged the same way (env var wins over the
        # secret; the secret wins over the empty default).
        if blob.get("API_BASE_URL") and not self.public_api_base_url:
            object.__setattr__(self, "public_api_base_url", blob["API_BASE_URL"])
        # Round 5B item 13: the CDK sets these as PLAIN task env vars (no
        # LUCIEL_ prefix, matching the AWS-side names), so pydantic's prefixed
        # loader never sees them — bridge from os.environ, blob still filling
        # any remaining gap via the map above.
        for plain_env, ses_field in (
            ("SES_CONFIGURATION_SET", "ses_configuration_set"),
            ("SES_EVENTS_TOPIC_ARN", "ses_events_topic_arn"),
        ):
            if not getattr(self, ses_field) and os.environ.get(plain_env):
                object.__setattr__(self, ses_field, os.environ[plain_env])

        # LLM model IDs (§3.4.3 model-retirement contingency). These carry
        # non-empty defaults, so the generic empty-field overlay above never
        # reaches them — bridged explicitly with env > platform-secret > default
        # precedence. The point: when a provider retires a model version, the
        # swap is a platform-secret rotation + ECS restart, not an image
        # rebuild + deploy. A direct env var (local/CI, and any deliberate
        # task-definition pin) still wins over the blob.
        for env_name, blob_key, field_name in (
            (
                "LUCIEL_LLM_ANTHROPIC_PRIMARY_MODEL",
                "LLM_ANTHROPIC_PRIMARY_MODEL",
                "llm_anthropic_primary_model",
            ),
            (
                "LUCIEL_LLM_ANTHROPIC_FAST_MODEL",
                "LLM_ANTHROPIC_FAST_MODEL",
                "llm_anthropic_fast_model",
            ),
            (
                "LUCIEL_LLM_OPENAI_FALLBACK_MODEL",
                "LLM_OPENAI_FALLBACK_MODEL",
                "llm_openai_fallback_model",
            ),
        ):
            if os.environ.get(env_name):
                continue  # pydantic already applied the env value; env wins
            if blob.get(blob_key):
                object.__setattr__(self, field_name, blob[blob_key])

        # JWT signing key: security-critical. Bridge it from the platform secret; it has
        # a KNOWN dev-default (published in source), so in live mode we must FAIL CLOSED
        # if it wasn't overridden — a forgeable-session vuln must never reach a real env.
        if blob.get("JWT_SIGNING_KEY"):
            object.__setattr__(self, "jwt_signing_key", blob["JWT_SIGNING_KEY"])
        if blob.get("JWT_SIGNING_KEY_PREVIOUS"):
            object.__setattr__(self, "jwt_signing_key_previous", blob["JWT_SIGNING_KEY_PREVIOUS"])
        if (
            self.integration_mode == "live"
            and self.jwt_signing_key == "dev-only-not-a-secret-rotate-in-prod"
        ):
            raise RuntimeError(
                "JWT_SIGNING_KEY is still the insecure dev default in live mode — refusing "
                "to start (would make all session tokens forgeable). Set JWT_SIGNING_KEY "
                "in the platform secret."
            )

        # Stripe webhook signing secret: same class as the JWT key. The fake's secret is
        # published in this repo, so accepting it in live mode would let anyone forge a
        # billing event (state changes, dunning exits) — fail closed exactly as above.
        # STRIPE_TEST_WEBHOOK_SECRET is accepted as an alias so the "test" posture keys
        # already provisioned in the blob also arm the webhook route.
        for key in ("STRIPE_WEBHOOK_SECRET", "STRIPE_TEST_WEBHOOK_SECRET"):
            if blob.get(key):
                object.__setattr__(self, "stripe_webhook_secret", blob[key])
                break
        if self.integration_mode == "live" and self.stripe_webhook_secret in (
            "",
            FAKE_STRIPE_WEBHOOK_SECRET,
        ):
            raise RuntimeError(
                "STRIPE_WEBHOOK_SECRET is unset (or still the published test constant) in "
                "live mode — refusing to start (would make billing webhook events "
                "forgeable). Set STRIPE_WEBHOOK_SECRET in the platform secret."
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

        # Derive the session-cookie parent domain from APP_BASE_URL when the frontend and
        # API share a registrable domain but sit on different hosts (www./api.). Setting
        # Domain=.<parent> lets BOTH hosts read the session cookie (same-site, so
        # SameSite=Lax still applies) — otherwise the cookie is host-only to the API and
        # the frontend bounces logged-in users back to /login. Skipped for localhost/IPs
        # (no dotted registrable domain) and never overrides an explicit cookie_domain.
        if not self.cookie_domain:
            base = str(blob.get("APP_BASE_URL") or "").strip()
            host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            labels = host.split(".")
            is_ip = host.replace(".", "").isdigit()
            if host and not is_ip and host != "localhost" and len(labels) >= 2:
                # Registrable domain = last two labels (api.vantagemind.ai ->
                # vantagemind.ai; vantagemind.ai -> vantagemind.ai). Good enough for the
                # single-level TLDs this product uses.
                parent = ".".join(labels[-2:])
                object.__setattr__(self, "cookie_domain", "." + parent)
        for blob_key, attr in overlay.items():
            if not getattr(self, attr, "") and blob.get(blob_key):
                object.__setattr__(self, attr, blob[blob_key])

        # Delivery/verification secrets, checked AFTER the overlay bridge above so the
        # blob has already been applied. These fail closed in live mode for two distinct
        # reasons, both of which are silent in a running system:
        #
        #  * TWILIO_AUTH_TOKEN / META_APP_SECRET are the HMAC keys the inbound channel
        #    webhooks verify with. Empty-string keys do not disable verification — they
        #    make it succeed for anyone who can compute an HMAC with a key they also
        #    know is "" (P1-9). A forged webhook is an inbound message attributed to a
        #    tenant.
        #  * PLATFORM_EMAIL_SENDER decides whether ``build_email_sender`` binds SES or
        #    the Null sender. Unset, every platform email (verification, reset, dunning,
        #    escalation, export-ready) is recorded and dropped (P1-10).
        if self.integration_mode == "live":
            missing = [
                name
                for name, value in (
                    ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                    ("META_APP_SECRET", self.meta_app_secret),
                    ("PLATFORM_EMAIL_SENDER", self.platform_email_sender),
                )
                if not value
            ]
            # INSTAGRAM_APP_SECRET is conditional, and the condition is the point. An
            # absent Instagram app is a supported posture — the connection reports
            # configured=false and the owner sees a disabled tile. A HALF-provisioned one
            # is not: the authorize URL builds from the app id and sends the owner to a
            # real consent screen whose code can then never be redeemed.
            if (self.instagram_app_id or self.instagram_redirect_uri) and not (
                self.instagram_app_secret
            ):
                missing.append("INSTAGRAM_APP_SECRET")
            # Same half-provisioned rule for the Microsoft (Outlook mailbox) app:
            # absent entirely = honestly unconfigured; id/redirect without the secret
            # sends the owner through a real consent screen to an unredeemable code.
            if (self.microsoft_client_id or self.microsoft_redirect_uri) and not (
                self.microsoft_client_secret
            ):
                missing.append("MICROSOFT_CLIENT_SECRET")
            if missing:
                raise RuntimeError(
                    "live mode is missing required platform secret(s): "
                    f"{', '.join(missing)} — refusing to start (unset webhook HMAC keys "
                    "accept forged inbound messages; an unset sender identity drops every "
                    "platform email). Set them in the platform secret."
                )

        # Bool bridge (non-production seam): the platform secret may carry the
        # captcha-bypass flag as a string; coerce it. Absent -> stays False (fail-safe).
        bypass_raw = blob.get("ALLOW_CAPTCHA_BYPASS_TOKEN")
        if bypass_raw is not None and not self.allow_captcha_bypass_token:
            object.__setattr__(
                self,
                "allow_captcha_bypass_token",
                str(bypass_raw).strip().lower() in {"1", "true", "yes", "on"},
            )


class PlatformSecretError(RuntimeError):
    """The platform secret blob exists but could not be read (live mode only)."""


def parse_platform_secret(raw: str, *, live: bool = False) -> dict[str, str]:
    """Parse the LUCIEL_PLATFORM_SECRET JSON blob into a flat str->str mapping.

    The blob is injected from AWS Secrets Manager and holds the platform LLM
    keys (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY). Returns an empty dict for the
    "{}" placeholder or any empty input so "mock" mode never faults.

    MALFORMED input (P2-12) is different from ABSENT input. A truncated or
    corrupted blob silently disabled every platform credential at once — the LLM
    keys, the Stripe secret, the OAuth app clients — and the only trace was one
    warning line. Downstream that reads as "not configured", which every seam is
    built to degrade past quietly. In live mode a blob we cannot read is a boot
    failure, not a degradation.

    SECURITY: the parsed values are credentials — this function NEVER logs them.
    On a parse error it logs only the failure fact, not the input (Space
    Instructions §3.4 / Architecture §3.8.3)."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Never log ``raw`` — it may contain secret material.
        if live:
            raise PlatformSecretError(
                "LUCIEL_PLATFORM_SECRET is not valid JSON — refusing to start in live mode. "
                "Treating it as empty would silently disable every platform credential."
            ) from None
        logger.warning("LUCIEL_PLATFORM_SECRET is not valid JSON; treating as empty")
        return {}
    if not isinstance(parsed, dict):
        if live:
            raise PlatformSecretError(
                "LUCIEL_PLATFORM_SECRET did not decode to a JSON object — refusing to start "
                "in live mode."
            )
        logger.warning("LUCIEL_PLATFORM_SECRET did not decode to an object; treating as empty")
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def live_mode() -> bool:
    """True when this process is bound to real providers rather than mock/CI stubs.

    The one predicate every honest-degradation seam asks. Kept here so a module can ask
    it without importing anything heavier than config.
    """
    return get_settings().integration_mode == "live"
