# app/connections — CLAUDE.md

## Purpose
Manages BYO external-provider identities, OAuth, lifecycle state, health, deferred work, registry, and secrets.

## Key files
`service.py` — connection lifecycle operations.
`registry.py` — supported provider definitions.
`oauth_flow.py` — signed OAuth state and callback exchange.
`verify.py` — post-exchange provider verification for EVERY OAuth provider (identity call, scope check, expiry stamp, `connectedAs`); `probes.py` — live liveness probes the health sweep binds (round 6 WP-C).
`secrets.py` — tenant secret storage.
`health.py`, `gate.py`, and `deferred_queue.py` — readiness and follow-up.

## Contracts this module owns
- Connection lifecycle: Connect → healthy / Action needed / Reconnect → Disconnect → Switch; swap keeps old credentials serving until verification.
- Store tenant secrets under `vm/`; disconnect clears credentials and is audited.
- Be honestly disabled/action-needed for unconfigured providers or unbound channel destinations.
- `list_connections` (GET /connections, the Overview list) hides every NOT_SERVING row — revoked
  and a deleted Luciel's `quiescing` ones alike (the raw drain status is not a wire status); the
  rows reappear with their stashed status on restore. `_single_active` still finds a quiescing
  row so a re-attach during grace reuses the type's single slot.

## Testing pointers
- `tests/contract/test_connection_lifecycle.py`, `test_connections.py`, and OAuth callback tests.
- `tests/integration/test_connections.py`, swap, provisioning, operability, and deferred-health tests.

## Gotchas from the 2026-08-06 truth delta (updated 2026-08-10)
- One Meta connection covers WhatsApp/Messenger; Instagram is separate and each channel needs its own destination binding.
- BYO email mailbox IS now built (Outlook first, owner decision 2026-08-10): `outlook`
  registers under the existing `email_sender` type beside the platform `ses` modes.
  Connect-verify-bind-live runs through `complete_oauth_connect`'s verify seam
  (`verify.py`); a ses→outlook swap derives its flow shape from the STAGED provider
  and cuts over (provider + auth_class) only in `confirm_reconnect`. The SES sender
  keeps serving on any verify failure — never silently replaced.
- `provision_email` binds the address as the inbound-routing `destination` (with the
  cross-tenant uniqueness check); before migration 0037 no email_sender row carried
  one and SES inbound resolved no tenant at all.
- Microsoft tokens live ~1h and refresh tokens ROTATE: `app/channels/tenant_outlook.py`
  owns refresh (on-demand + the health sweep's provider-dispatching refresher). Live
  mode only; mock falls back to the honest no-refresher.

## Provider app registrations (round 7 WP-7, 2026-10-19)
- Meta Graph version: ONE setting, `Settings.meta_graph_version` (default v23.0; env
  `LUCIEL_META_GRAPH_VERSION` > platform secret `META_GRAPH_VERSION`). Build URLs with
  `config.meta_graph_base(host)`; the registry stores `{meta_graph_version}` templates that
  `get_provider` resolves on read. Never write a versioned graph URL anywhere else
  (`tests/unit/test_meta_graph_version.py` scans app/).
- Redirect URIs: `Settings.oauth_redirect_problems()` — each configured URI must end with
  `/api/v1/connections/<oauth client label>/callback` (and equal `public_api_base_url` +
  path when that is set). A bad one is logged at boot, makes `authorize_ready` false
  (provider withheld, no authorize URL; refresh untouched) and shows on /healthz as
  `oauth_redirect:<label>`. `config.OAUTH_REDIRECT_URI_FIELDS` mirrors the registry labels.
- Login host: `ProviderSpec.login_hosts` (Salesforce: production, sandbox). The connect body's
  `loginHost` is validated (`validated_login_host`) and stored as `non_secret_config.login_host`;
  authorize (`build_authorize_start(login_host=)`), the callback's exchange
  (`registry.login_host_scope`) and refresh (`resolve_provider_endpoint`) all read it.
- Meta app posture: a provisioned Meta/Instagram app needs META_VERIFY_TOKEN (live boot
  refuses; /healthz `meta_webhook:unconfigured`), and META_APP_SECRET only when the Meta app
  is provisioned; an empty HMAC key verifies nothing (`signatures.verify_hmac_shared_secret`).
- Platform-secret keys: `config.PLATFORM_SECRET_KEYS` is the one accepted list
  (`scripts/merge_platform_secret.py` imports it; `--check` reports unread keys, redirect
  problems and Meta webhook gaps for a blob before a deploy).

## Change checklist
- Read the root `CLAUDE.md` and trace callers and dependents before editing.
- Add or update a test that reproduces the real condition being changed.
- Keep tenant, live/mock, and externally visible status behavior explicit.
- If persistence, routes, or deployment posture changes, update the related migration, registry, or infrastructure test deliberately.
- Do not replace an error/unknown state with fabricated success.

## Review prompt
Before merge, ask whether this change could hide an unconfigured, failed,
or tenant-crossing condition behind a successful-looking result. If so,
make that state explicit and test it.
