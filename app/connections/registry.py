"""The provider REGISTRY: one declarative table behind every connection (§3.8.2/§3.8.3).

Before this module, "which providers exist for a connection type" was spread across
three places that had to be edited together — the OAuth authorize table in
``oauth_flow``, the ``if provider == ...`` client-credential chain in
``knowledge.providers``, and the frontend's hardcoded vendor pins. Adding a CRM meant
touching all three, so in practice the product shipped with push_to_crm nailed to
HubSpot and record_source nailed to Salesforce (Decision #6).

Here every provider is ONE ``ProviderSpec`` entry carrying its connection type, auth
kind, OAuth endpoint/scopes/PKCE posture, the settings field names its app credential
is read from, its health-probe class, and (knowledge sources) the kind of scope the
owner may narrow it to. Registering a dict entry is the whole job of adding a provider
— the connect-start, credential lookup, health cadence, and the provider list the UI
renders all derive from this table.

Secret hygiene (§3.8.3) is preserved: the registry stores the NAMES of settings fields,
never credential values. Per-tenant tokens continue to live only in the SecretStore.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.config import get_settings

#: The slot a registry endpoint carries where the Meta Graph API version goes. The
#: version is a SETTING (``meta_graph_version``, round 7 WP-7), so the table stores the
#: template and ``get_provider`` fills it at read time — no version literal lives here.
GRAPH_VERSION_SLOT = "{meta_graph_version}"


@dataclass(frozen=True)
class OAuthClientConfig:
    """The application's OAuth-client registration for one provider (§3.8.3: this
    is NOT the per-tenant token — it's who the app claims to be to the provider)."""

    client_id: str
    client_secret: str
    redirect_uri: str
    # False when the configured redirect URI is not one the callback route serves
    # (``Settings.oauth_redirect_problems``, round 7 WP-7): a consent started on it
    # ends on a 404 or a refused redeem, so the provider is not offered.
    redirect_valid: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def authorize_ready(self) -> bool:
        """Everything an authorize START needs — mirrors build_authorize_start's gate.

        ``configured`` (id+secret) is enough for token REFRESH, but starting a new
        consent also needs the registered redirect URI (oauth_flow refuses without
        it). Availability surfaces must use THIS, or a provider with id+secret and
        no redirect renders an enabled Connect button that dead-ends on the
        placeholder URL (GAP-N3, round 4). A redirect URI the callback route cannot
        serve counts as absent (round 7 WP-7)."""
        return self.configured and bool(self.redirect_uri) and self.redirect_valid


@dataclass(frozen=True)
class ProviderAuthorize:
    """One provider's authorize endpoint + the scopes this platform requests."""

    endpoint: str
    scopes: str
    supports_pkce: bool
    # Where the ``code`` is redeemed. Registering it here (rather than in a map beside
    # the exchange code) is what makes a new OAuth provider pure configuration.
    exchange_endpoint: str | None = None
    # Provider-specific params that are part of OUR request, not the user's choice
    # (e.g. Google only returns a refresh_token for an offline+consent request).
    extra: dict[str, str] = field(default_factory=dict)
    # Response keys BEYOND access_token/refresh_token that this provider returns and we
    # must keep (Salesforce's org API base, Instagram's account id). Declared here so the
    # one exchange function stays provider-agnostic.
    token_fields: tuple[str, ...] = ()
    # Meta's token-exchange family issues a SHORT-lived token from the code and requires a
    # second call to upgrade it. Set both to make the exchange perform that upgrade; the
    # short-lived token is then never stored, because it expires in about an hour.
    long_lived_endpoint: str | None = None
    long_lived_grant_type: str | None = None
    # How the client credential authenticates the token-exchange request (RFC 6749
    # client auth): "post" sends client_id/client_secret as body fields
    # (client_secret_post — the default every existing provider uses); "basic" sends
    # an HTTP Basic Authorization header instead (client_secret_basic — Notion
    # REQUIRES this and rejects body credentials). Declared here so the one exchange
    # function stays provider-agnostic (same doctrine as token_fields above).
    exchange_client_auth: str = "post"
    # The exchange request body encoding: "form" (x-www-form-urlencoded, the OAuth
    # default) or "json" (Notion's token endpoint takes a JSON body).
    exchange_body: str = "form"
    # Scopes the grant MUST carry for the effect layer to work (round 6 WP-C). The
    # post-exchange verifier refuses a narrower grant with the fix in the reason; a
    # provider whose token report carries no scope list leaves this empty.
    required_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CredentialField:
    """One field a non-OAuth provider's connect form collects.

    ``secret=True`` fields are handed to the SecretStore and never persisted on the
    connection row; ``secret=False`` fields land in ``non_secret_config`` (§3.8.3)."""

    name: str
    label: str
    secret: bool = False
    required: bool = True


# Auth kinds drive the connect-flow SHAPE returned by start_connection.
AUTH_OAUTH = "oauth"
AUTH_CREDENTIAL_FORM = "credential_form"
AUTH_PROVISIONED = "provisioned"

# Scope kinds (Decision #9): what the owner may narrow a knowledge source down to.
SCOPE_DRIVE_FOLDERS = "drive_folders"
SCOPE_NOTION_PAGES = "notion_pages"


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the platform needs to know about one selectable provider."""

    provider: str
    connection_type: str
    display_name: str
    auth_kind: str
    # OAuth-kind providers only: the consent endpoint, scopes, and PKCE posture.
    authorize: ProviderAuthorize | None = None
    # Settings FIELD NAMES (never values) the app credential is read from (§3.8.3).
    client_id_setting: str | None = None
    client_secret_setting: str | None = None
    redirect_uri_setting: str | None = None
    # Non-OAuth providers: the fields their connect form collects.
    credential_fields: tuple[CredentialField, ...] = ()
    # §3.8.5: auth_class is the behavior key the connection row inherits (what the
    # health probe and the token refresh DO for it — the sweep itself is one 6-hour
    # cron for every class), so a new provider in an existing class needs no code.
    auth_class: str = "oauth_token"
    # Knowledge sources: the scope the owner may select (Decision #9). None = no scope.
    scope_kind: str | None = None
    # Human-readable one-liner the UI shows next to the choice.
    help_text: str = ""
    # Connection types for which this provider is offered but its EFFECT layer is
    # not built yet (honest-disabled: served configured=false, so the UI renders
    # the "not available yet" row instead of a connect button that dead-ends).
    # Scoped per type because one provider can be real on one surface and not
    # another — Salesforce KB-read works; Salesforce lead-push does not.
    unavailable_for: frozenset[str] = frozenset()
    # OAuth-kind providers whose ``credential_fields`` form is a full, standalone
    # connect path (audit round 3, C10 — Twilio). Availability then must NOT
    # depend on the platform's OAuth app being provisioned: the customer's own
    # credential needs nothing from us, so the provider stays available and the
    # start flow degrades to the form when the OAuth client is unconfigured.
    credential_fallback: bool = False

    @property
    def is_oauth(self) -> bool:
        return self.auth_kind == AUTH_OAUTH


PROVIDERS: dict[str, ProviderSpec] = {
    # --- Calendar (Decision #6: google_calendar | calendly) ------------------------
    "google_calendar": ProviderSpec(
        provider="google_calendar",
        connection_type="calendar",
        display_name="Google Calendar",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_endpoint="https://oauth2.googleapis.com/token",
            scopes="https://www.googleapis.com/auth/calendar.events",
            supports_pkce=True,
            extra={
                "access_type": "offline",
                "prompt": "consent",
                # Same Google client as Drive, so an owner who already granted Drive is
                # doing INCREMENTAL auth here: without this Google issues a token
                # carrying only calendar.events and the Drive grant silently narrows.
                "include_granted_scopes": "true",
            },
        ),
        # The SHARED Google client (see Settings.google_client_id) — Calendar is a scope
        # on the same OAuth app as Drive, not a separate app. Only the scope differs.
        client_id_setting="google_client_id",
        client_secret_setting="google_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="google_redirect_uri",
        help_text="Luciel reads your availability and writes bookings into this calendar.",
    ),
    "calendly": ProviderSpec(
        provider="calendly",
        connection_type="calendar",
        display_name="Calendly",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://auth.calendly.com/oauth/authorize",
            exchange_endpoint="https://auth.calendly.com/oauth/token",
            scopes="default",
            supports_pkce=True,
        ),
        client_id_setting="calendly_client_id",
        client_secret_setting="calendly_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="calendly_redirect_uri",
        help_text=(
            "Offer times from your Calendly availability; customers book through a "
            "Calendly link for the exact slot (round 6 WP-H)."
        ),
    ),
    # --- CRM (Decision #6: hubspot | salesforce | custom_webhook) -----------------
    "hubspot": ProviderSpec(
        provider="hubspot",
        connection_type="crm",
        display_name="HubSpot",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://app.hubspot.com/oauth/authorize",
            exchange_endpoint="https://api.hubapi.com/oauth/v1/token",
            # The push WRITES contacts (crm_backend.HubSpotCrmApi.create/update); a
            # read-only grant connected cleanly and failed on the first lead (round 6
            # WP-C). The HubSpot app registration must carry the same scope list.
            scopes="crm.objects.contacts.read crm.objects.contacts.write "
            "crm.objects.companies.read crm.objects.deals.read",
            required_scopes=("crm.objects.contacts.read", "crm.objects.contacts.write"),
            # HubSpot's authorize endpoint rejects unknown query params, and it does not
            # implement PKCE — sending a code_challenge fails the request outright.
            supports_pkce=False,
        ),
        client_id_setting="hubspot_client_id",
        client_secret_setting="hubspot_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="hubspot_redirect_uri",
        help_text="Push captured leads into HubSpot contacts.",
    ),
    "salesforce": ProviderSpec(
        provider="salesforce",
        connection_type="crm",
        display_name="Salesforce",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://login.salesforce.com/services/oauth2/authorize",
            exchange_endpoint="https://login.salesforce.com/services/oauth2/token",
            scopes="api refresh_token",
            supports_pkce=True,
            token_fields=("instance_url",),  # the org's API base, needed on every call
        ),
        client_id_setting="salesforce_consumer_key",
        client_secret_setting="salesforce_consumer_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="salesforce_redirect_uri",
        # Lead-push is REAL since Phase 6.5 (owner decision 2026-08-23):
        # SalesforceCrmBackend upserts Lead records in the tenant's own org
        # (instance_url from token_fields) with the same link/heal discipline
        # as HubSpot — the crm unavailable_for scoping is gone.
        help_text="Push captured leads into Salesforce Leads.",
    ),
    "custom_webhook": ProviderSpec(
        provider="custom_webhook",
        connection_type="crm",
        display_name="Custom webhook",
        auth_kind=AUTH_CREDENTIAL_FORM,
        credential_fields=(
            CredentialField("url", "Webhook URL"),
            CredentialField("auth_header", "Authorization header", secret=True, required=False),
        ),
        auth_class="long_lived_token",
        help_text="Post captured leads to your own endpoint — for any CRM we don't "
        "connect to natively.",
    ),
    # --- Record source (Decision #6: live_connector | csv) ------------------------
    "live_connector": ProviderSpec(
        provider="live_connector",
        connection_type="record_source",
        display_name="Live connector",
        auth_kind=AUTH_CREDENTIAL_FORM,
        credential_fields=(
            CredentialField("url", "Record lookup URL"),
            CredentialField("api_key", "API key", secret=True, required=False),
        ),
        auth_class="long_lived_token",
        help_text=(
            "Look records up live from your own system: an https endpoint that answers "
            "GET ?q=<query> with a JSON array of records (round 6 WP-H)."
        ),
    ),
    "csv": ProviderSpec(
        provider="csv",
        connection_type="record_source",
        display_name="CSV upload",
        auth_kind=AUTH_PROVISIONED,
        auth_class="provisioned_resource",
        help_text="Upload a spreadsheet of records; re-upload to refresh.",
    ),
    # --- Knowledge sources (Decision #9 adds selectable scope) --------------------
    "google_drive": ProviderSpec(
        provider="google_drive",
        connection_type="knowledge_source",
        display_name="Google Drive",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_endpoint="https://oauth2.googleapis.com/token",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            supports_pkce=True,
            extra={
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            },
        ),
        client_id_setting="google_client_id",
        client_secret_setting="google_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="google_redirect_uri",
        scope_kind=SCOPE_DRIVE_FOLDERS,
        help_text="Choose which folders Luciel may read.",
    ),
    "notion": ProviderSpec(
        provider="notion",
        connection_type="knowledge_source",
        display_name="Notion",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://api.notion.com/v1/oauth/authorize",
            exchange_endpoint="https://api.notion.com/v1/oauth/token",
            scopes="",
            supports_pkce=False,
            extra={"owner": "user"},
            # Notion's /v1/oauth/token requires HTTP Basic client auth with a JSON
            # body; the default form-encoded client_secret_post is rejected with a
            # 401 (live-caught in the round-4 wiring review before any tenant hit it).
            exchange_client_auth="basic",
            exchange_body="json",
        ),
        client_id_setting="notion_client_id",
        client_secret_setting="notion_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="notion_redirect_uri",
        scope_kind=SCOPE_NOTION_PAGES,
        help_text="Choose which pages or databases Luciel may read.",
    ),
    # --- Meta channels (Decision #7) ----------------------------------------------
    # ONE Facebook grant, TWO channels: WhatsApp and Messenger both bind to the same Meta
    # Business account (§3.1.4/§3.8.1), and ``instance_connections`` allows one active row
    # per connection_type — so modelling them as separate providers meant connecting one
    # silently evicted the other. A single Meta OAuth grant carrying the union of their
    # scopes authorizes both; which asset each ENABLED channel answers on is then named
    # per channel via PUT /destination.
    #
    # Instagram is NOT on this grant. Facebook rejects the whole authorize request with
    # "Invalid Scopes: instagram_basic, instagram_manage_messages" — under Meta's
    # use-case app model, Instagram messaging is a separate product ("Instagram API with
    # Business Login for Instagram") with its own app id and its own OAuth host. Asking
    # for those scopes here does not degrade to a partial grant; it fails the dialog
    # outright, so WhatsApp and Messenger could not be connected either. See the
    # ``instagram`` entry below.
    "meta": ProviderSpec(
        provider="meta",
        connection_type="channel_auth",
        # Just "Meta": since the §3.1.2 channel split the WhatsApp and Messenger rows each
        # carry their own name, so the shared grant's button reads "Connect Meta" instead
        # of repeating both channel names under whichever row rendered it.
        display_name="Meta",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint=f"https://www.facebook.com/{GRAPH_VERSION_SLOT}/dialog/oauth",
            exchange_endpoint=f"https://graph.facebook.com/{GRAPH_VERSION_SLOT}/oauth/access_token",
            scopes="whatsapp_business_messaging,whatsapp_business_management,"
            "pages_messaging,pages_show_list,business_management",
            # Meta's dialog/oauth does not implement PKCE; state carries the CSRF proof.
            supports_pkce=False,
            # Round 5B item 14: the code redeems to a SHORT-lived (~1-2h) user
            # token; the fb_exchange_token upgrade mints the ~60-day one. Without
            # this, WhatsApp/Messenger died an hour after connect and stayed dead
            # until the 6-hourly sweep happened to re-exchange — instagram below
            # always had its equivalent.
            long_lived_endpoint=f"https://graph.facebook.com/{GRAPH_VERSION_SLOT}/oauth/access_token",
            long_lived_grant_type="fb_exchange_token",  # noqa: S106 — a grant type
        ),
        client_id_setting="meta_app_id",
        client_secret_setting="meta_app_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="meta_redirect_uri",
        help_text="Connect your Meta Business account once — it powers WhatsApp and "
        "Facebook Messenger. Instagram is connected separately.",
    ),
    # --- Instagram (Business Login for Instagram) ----------------------------------
    # A SECOND connection type rather than a second ``channel_auth`` provider, and that is
    # forced by the storage model, not a preference: ``_single_active`` and
    # ``secret_ref_for`` are both keyed on connection_type alone, so a second channel_auth
    # row would collide on the secret ref and make the single-active lookup ambiguous —
    # reintroducing exactly the eviction the single-Meta correction removed. Instagram
    # genuinely IS a separate grant (separate app id, separate OAuth host, separate token
    # endpoint), so it gets its own row and the two coexist.
    "instagram": ProviderSpec(
        provider="instagram",
        connection_type="instagram_auth",
        display_name="Instagram",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://www.instagram.com/oauth/authorize",
            exchange_endpoint="https://api.instagram.com/oauth/access_token",
            scopes="instagram_business_basic,instagram_business_manage_messages",
            supports_pkce=False,
            # The code redeems to a ~1-hour token; graph.instagram.com upgrades it to a
            # 60-day one. Storing the short-lived token would leave every tenant
            # disconnected an hour after they connected.
            long_lived_endpoint="https://graph.instagram.com/access_token",
            long_lived_grant_type="ig_exchange_token",  # noqa: S106 — a grant type
            # Which IG account authorized; the owner still names the asset via
            # PUT /destination, but this is what lets us tell them if they picked another.
            token_fields=("user_id",),
        ),
        client_id_setting="instagram_app_id",
        client_secret_setting="instagram_app_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="instagram_redirect_uri",
        help_text="Connect the Instagram professional account Luciel answers DMs on.",
    ),
    # --- Email (§3.1.6a): platform-provisioned SES today, BYO mailbox as the ruled
    # --- target. Outlook is the first BYO-mailbox provider (owner decision
    # --- 2026-08-10; Gmail follows once Google's restricted-scope review is done).
    "outlook": ProviderSpec(
        provider="outlook",
        connection_type="email_sender",
        display_name="Outlook mailbox",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            # /common serves both org (Entra) and personal Microsoft accounts — the
            # Azure app registration must be "any org directory + personal accounts".
            endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            exchange_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            # offline_access is what returns a refresh token; Microsoft access tokens
            # live ~1 hour, so without it every mailbox dies mid-afternoon.
            scopes="offline_access User.Read Mail.Read Mail.Send",
            supports_pkce=True,
            extra={"response_mode": "query", "prompt": "select_account"},
            # Seconds-until-expiry of the access token. Kept so the verify step can
            # stamp non_secret_config["token_expires_at"] — the refresh path's cue.
            token_fields=("expires_in",),
        ),
        client_id_setting="microsoft_client_id",
        client_secret_setting="microsoft_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="microsoft_redirect_uri",
        help_text="Connect the work mailbox Luciel answers from — replies come from "
        "your own address. Sent mail lands in your own Sent folder.",
    ),
    # --- Senders (provisioned / platform-owned, no owner-facing choice) -----------
    "ses": ProviderSpec(
        provider="ses",
        connection_type="email_sender",
        # Harmony wave 2 (item 4): this leaked to the owner as the raw provider
        # code "Ses" (Overview composed "Email sending · Ses" with nothing else to
        # render, since ConnectionOut carried no display label at all). "Amazon
        # SES" is the actual vendor VantageMind provisions this sender through --
        # naming it plainly here means Overview never has to derive a label from
        # the raw code again.
        display_name="Amazon SES",
        auth_kind=AUTH_PROVISIONED,
        auth_class="api_key",
        help_text="The address Luciel sends and receives on.",
    ),
    # BYO telephony (§3.1.4): the customer connects their OWN Twilio account and Luciel
    # texts/calls through it. VantageMind is never in the telephony billing path, which is
    # only true if the credential on the send path is theirs. One-click OAuth is the
    # PRIMARY connect (audit round 3, C10 — owner decision 2026-08-23); the credential
    # form stays as the explicit "use API keys instead" fallback, which is why this
    # provider is ALWAYS available (``credential_fallback``): the customer's own API
    # keys need no platform OAuth app. An OAuth tenant's row carries auth_class
    # "oauth_token" (bearer sends + refresh + capability-URL inbound verification, C11);
    # a credential tenant's row is stamped back to "api_key" at submit_credentials.
    "twilio": ProviderSpec(
        provider="twilio",
        connection_type="sms_sender",
        display_name="Your Twilio account",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            # Twilio's OAuth 2.0 authorization-code flow ("organizational API
            # access"). Endpoints validated live 2026-08-24 (round 4): the OWNER'S
            # Twilio console emits authorize URIs on oauth.twilio.com/v2 for this
            # app class (OQ… client ids), the vendored twilio SDK's OAuth module
            # bases at oauth.twilio.com, and live probes confirm /v2/authorize
            # (400 on missing params) and /v2/token (405 on GET) are the live
            # pair. The previous login.twilio.com/oauth2/* pins matched neither
            # that console URI nor login.twilio.com's own OIDC metadata — they
            # were unvalidated configuration from C10.
            endpoint="https://oauth.twilio.com/v2/authorize",
            exchange_endpoint="https://oauth.twilio.com/v2/token",
            scopes="offline_access",
            supports_pkce=False,
        ),
        client_id_setting="twilio_oauth_client_id",
        client_secret_setting="twilio_oauth_client_secret",  # noqa: S106 — field NAME  # nosec B106
        redirect_uri_setting="twilio_oauth_redirect_uri",
        credential_fields=(
            CredentialField("accountSid", "Twilio Account SID (AC…)"),
            CredentialField("authToken", "Twilio Auth Token", secret=True, required=False),
            CredentialField("apiKeySid", "API Key SID (SK…)", required=False),
            CredentialField("apiKeySecret", "API Key Secret", secret=True, required=False),
        ),
        auth_class="oauth_token",
        help_text="Connect your Twilio account so Luciel can text from your business "
        "number. Carrier costs stay on your own Twilio bill.",
        credential_fallback=True,
    ),
    "outbound_webhook": ProviderSpec(
        provider="outbound_webhook",
        connection_type="outbound_webhook",
        display_name="Outbound webhook",
        auth_kind=AUTH_CREDENTIAL_FORM,
        credential_fields=(
            CredentialField("url", "Webhook URL"),
            CredentialField("auth_header", "Authorization header", secret=True, required=False),
        ),
        auth_class="long_lived_token",
        help_text="Send structured events to a URL you control.",
    ),
}

# Which providers the owner may CHOOSE between, per connection type, in display order.
# Derived from PROVIDERS so a new registration shows up in the UI with no extra edit.
_ORDER: dict[str, tuple[str, ...]] = {
    "calendar": ("google_calendar", "calendly"),
    "crm": ("hubspot", "salesforce", "custom_webhook"),
    "record_source": ("live_connector", "csv"),
    "knowledge_source": ("google_drive", "notion"),
    "channel_auth": ("meta",),
    "instagram_auth": ("instagram",),
    "sms_sender": ("twilio",),
    # The BYO webhook tool's endpoint registration (Phase 5): one provider, but
    # listing it makes /connections/providers serve its credential form — the
    # frontend previously had NO registry group at all and rendered a connect
    # button with no URL field.
    "outbound_webhook": ("outbound_webhook",),
    # The choice here is not between vendors but between MODES: keep the platform
    # SES address (never offered as a pick — it is provisioned via /connections/email)
    # or connect your own Outlook mailbox. Only the BYO option is choosable.
    "email_sender": ("outlook",),
}

# Superseded provider ids. Rows already written under them keep resolving (and
# normalize to the registered id on the next connect) instead of turning into an
# unregistered provider with no config:
#   * ``meta_whatsapp`` / ``meta_instagram`` — split before the single-Meta correction;
#   * ``api`` — what the round 6 WP-H record-source dispatch and connect-time proof
#     keyed on, while the registry only ever offered ``live_connector`` (round 7
#     WP-4a item 1: the live connector was permanently green and permanently dead).
_ALIASES: dict[str, str] = {
    "meta_whatsapp": "meta",
    "meta_instagram": "meta",
    "api": "live_connector",
}


def canonical_provider(provider: str) -> str:
    """The registered id for a provider string, collapsing superseded aliases."""
    return _ALIASES.get(provider, provider)


# The connection types that present the owner a provider choice (Decision #6).
CHOICE_TYPES: frozenset[str] = frozenset(_ORDER)

# Providers whose whole credential is an https URL plus an OPTIONAL Authorization
# header (round 7 WP-4a items 4/6): proven at connect time by a GET the endpoint must
# not refuse, and probed by the sweep the same way. ``live_connector`` is URL-shaped
# too but carries its own record-array proof (item 1).
URL_SHAPED_PROVIDERS: frozenset[str] = frozenset({"custom_webhook", "outbound_webhook"})

# Providers ACCEPTED for a connection type but deliberately absent from its owner-facing
# choice list. Knowledge sync (§3.2.3) rides ``knowledge_source`` connections for two cases
# that are not "pick a knowledge provider" options: the CRM knowledge base (same vendor
# grant as the CRM connection, reached via the crm_kb origin) and website_crawl, which has
# no external account at all — it is an httpx fetch target. Accepting them here keeps the
# validation gate honest without inventing UI choices that do not exist.
_ALSO_ACCEPTED: dict[str, frozenset[str]] = {
    "knowledge_source": frozenset({"hubspot", "salesforce", "crm_kb", "website_crawl"}),
}


def resolve_endpoint(url: str) -> str:
    """A registry endpoint with its setting-driven slots filled (the Graph version)."""
    if GRAPH_VERSION_SLOT in url:
        url = url.replace(GRAPH_VERSION_SLOT, get_settings().meta_graph_version)
    return url


def _resolved(spec: ProviderSpec) -> ProviderSpec:
    """``spec`` with every authorize endpoint resolved against the running settings.

    The table holds templates (``GRAPH_VERSION_SLOT``); what a caller reads must be a
    real URL, so every lookup goes through here."""
    authorize = spec.authorize
    if authorize is None:
        return spec
    endpoint = resolve_endpoint(authorize.endpoint)
    exchange = authorize.exchange_endpoint and resolve_endpoint(authorize.exchange_endpoint)
    long_lived = authorize.long_lived_endpoint and resolve_endpoint(authorize.long_lived_endpoint)
    if (endpoint, exchange, long_lived) == (
        authorize.endpoint,
        authorize.exchange_endpoint,
        authorize.long_lived_endpoint,
    ):
        return spec
    return replace(
        spec,
        authorize=replace(
            authorize,
            endpoint=endpoint,
            exchange_endpoint=exchange,
            long_lived_endpoint=long_lived,
        ),
    )


def get_provider(provider: str) -> ProviderSpec | None:
    """The spec for a provider id (endpoints resolved), or None if it is not registered."""
    spec = PROVIDERS.get(canonical_provider(provider))
    return _resolved(spec) if spec is not None else None


def providers_for(connection_type: str) -> list[ProviderSpec]:
    """The selectable providers for a connection type, in owner-facing display order.

    Types absent from ``_ORDER`` (the provisioned senders) fall back to whatever is
    registered for them, so the function is total over the catalog."""
    ordered = _ORDER.get(connection_type)
    if ordered is not None:
        return [_resolved(PROVIDERS[p]) for p in ordered if p in PROVIDERS]
    return [_resolved(s) for s in PROVIDERS.values() if s.connection_type == connection_type]


def default_provider(connection_type: str) -> str | None:
    """The provider used when the caller does not choose one (first in display order)."""
    options = providers_for(connection_type)
    return options[0].provider if options else None


def has_choice(connection_type: str) -> bool:
    """True iff this type presents the owner a PROVIDER CHOICE (Decision #6).

    Only these types are validated against the registry on connect. Every type now
    carries a choice — ``email_sender`` joined when the BYO Outlook mailbox landed
    (its ``ses`` rows keep validating because ``ses`` is registered for the type).
    Everywhere a choice exists the string steers a real OAuth endpoint or credential
    form, so it is validated."""
    return connection_type in _ORDER


def is_valid_provider(connection_type: str, provider: str) -> bool:
    """True iff ``provider`` is registered FOR this connection type.

    Gate every provider selection through this: it is what stops an admin-supplied
    string from steering a connect flow at an arbitrary endpoint."""
    if provider in _ALSO_ACCEPTED.get(connection_type, frozenset()):
        return True
    spec = get_provider(provider)
    return spec is not None and spec.connection_type == connection_type


def provider_available(provider: str, connection_type: str | None = None) -> bool:
    """True iff a connection to this provider can actually be (re)established today.

    Harmony wave 2 (item 3): a stale ``instance_connections`` row can outlive the
    provider it points at -- the row was created when the provider was offered (or
    briefly misconfigured), and since then either the provider was dropped from the
    catalog entirely, or it is still catalogued but its platform OAuth client was
    never registered (missing client id/secret settings). Both cases are the SAME
    "not available yet" state the Configure page already derives via
    ``oauth_client_config(...).configured`` for ``list_providers`` -- this gives the
    Overview list (``list_connections`` / ``ConnectionOut``) the identical truth so
    the two pages cannot disagree about the same provider ever again."""
    spec = get_provider(provider)
    if spec is None:
        return False
    # unavailable_for wins over everything else (audit round 3, Phase 4): a
    # provider whose EFFECT layer is unbuilt for this surface is not available
    # there even with a registered OAuth client — otherwise provisioning creds
    # would light a consent flow with a dead tool behind it. Scoped per surface:
    # the SAME provider can stay live for another connection type (Salesforce KB
    # read vs. its unbuilt CRM push). Callers that know the row's type pass it;
    # with no type given, the spec's own primary type is the surface asked about.
    if (connection_type or spec.connection_type) in spec.unavailable_for:
        return False
    if not spec.is_oauth:
        return True
    # A standalone credential-form fallback (Twilio, C10) keeps the provider
    # available with no platform OAuth app: the customer's own API keys need
    # nothing from us, so "configured" does not gate this provider.
    if spec.credential_fallback:
        return True
    # authorize_ready, not configured (GAP-N3, round 4): id+secret without a
    # redirect URI cannot START a consent, so advertising it as available ships a
    # Connect button that dead-ends. Refresh paths keep using `configured` — an
    # already-granted token needs no redirect to renew.
    return oauth_client_config(spec.provider).authorize_ready


def secret_material_expected(provider: str, auth_class: str) -> bool:
    """True iff a connected row of this provider MUST hold secret material —
    i.e. an unresolvable/empty secret is evidence of LOSS, not a legitimate
    shape (round 5B item 11, §3.8.5 probe honesty).

    - Provisioned providers (ses, csv): platform-owned, no tenant secret —
      False. (Legacy ses rows carry auth_class api_key with a stamped-but-empty
      secret_ref; the SPEC lookup is what catches them.)
    - Credential-form providers: True only when some field is secret AND
      required. custom_webhook / outbound_webhook / live_connector collect only
      OPTIONAL secrets (the URL can be the whole credential), so an empty store
      is a legitimate connected shape — live-caught 2026-08-24 when the sweep
      false-errored exactly such a row.
    - OAuth providers: True — a connected row stored a token at exchange, and
      the Twilio credential fallback always stores material too.
    - Unregistered provider: fall back on auth_class alone.
    """
    if canonical_provider(provider) == "website_crawl":
        # An httpx fetch target, not an account: there is no credential to lose
        # (2026-09-05 audit F104 — the probe false-errored every crawl connection).
        return False
    spec = get_provider(provider)
    if spec is not None:
        if spec.auth_kind == AUTH_PROVISIONED:
            return False
        if spec.auth_kind == AUTH_CREDENTIAL_FORM:
            return any(f.secret and f.required for f in spec.credential_fields)
        return True
    return auth_class != "provisioned_resource"


def oauth_client_label(provider: str) -> str:
    """The OAuth APP this provider authenticates against, e.g. ``google``, ``salesforce``.

    Providers that share one registered OAuth app share a label — Drive and Calendar are
    two scopes on the SAME Google client (§3.8), so both resolve to ``google``. That is
    also why they share one redirect URI, and therefore why the provider segment of the
    callback path is the client label and NOT the provider id. Derived from the settings
    field name so a new provider needs no entry here.

    Returns "" for unregistered/non-OAuth providers."""
    spec = get_provider(provider)
    setting = spec.client_id_setting if spec is not None else None
    if not setting:
        return ""
    # `_oauth_client_id` first: a provider whose OAuth app fields are namespaced
    # away from its API-key fields (twilio_oauth_client_id vs twilio_api_key_*,
    # C10) still gets the natural callback label ("twilio", not "twilio_oauth").
    for suffix in ("_oauth_client_id", "_client_id", "_consumer_key", "_app_id"):
        if setting.endswith(suffix):
            return setting[: -len(suffix)]
    return setting


def oauth_client_config(provider: str) -> OAuthClientConfig:
    """Read a provider's OAuth app registration from settings via the registry.

    The registry stores settings FIELD NAMES, so this resolves any registered provider
    without a per-provider branch — that is what makes adding a provider config rather
    than code. Unregistered/non-OAuth providers return an unconfigured (empty) config
    rather than raising, so the catalog can grow and a live flow fails closed."""
    spec = get_provider(provider)
    if spec is None:
        # §3.2.3 wire origin `crm_kb` resolves to the native-CRM default so the
        # knowledge-side CRM sync keeps its existing credential lookup.
        if provider == "crm_kb":
            spec = PROVIDERS["hubspot"]
        else:
            return OAuthClientConfig("", "", "")
    s = get_settings()

    def _read(name: str | None) -> str:
        return str(getattr(s, name, "") or "") if name else ""

    label = oauth_client_label(spec.provider)
    return OAuthClientConfig(
        _read(spec.client_id_setting),
        _read(spec.client_secret_setting),
        _read(spec.redirect_uri_setting),
        redirect_valid=label not in s.oauth_redirect_problems(),
    )


# Dependency cascade (Decision #5b): disconnecting a connection must visibly disable
# what depended on it, with a reason the owner can read — not leave a tool switched on
# that silently fails at dispatch. Maps connection_type -> the addon tool ids it powers.
DEPENDENT_TOOLS: dict[str, tuple[str, ...]] = {
    "calendar": (
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    ),
    "crm": ("push_to_crm",),
    "record_source": ("lookup_record",),
    "email_sender": ("send_email",),
    "sms_sender": ("send_sms",),
    "outbound_webhook": ("bring_your_own_webhook",),
}

# Maps connection_type -> the channel ids it powers, for the same cascade.
DEPENDENT_CHANNELS: dict[str, tuple[str, ...]] = {
    "email_sender": ("email",),
    "sms_sender": ("sms", "voice"),
    # §3.1.2 split: the one Facebook grant powers the WhatsApp AND Messenger channels
    # (each still binds its own destination id); Instagram rides its own Business Login
    # grant. Each channel therefore has exactly one backing connection.
    "channel_auth": ("whatsapp", "messenger"),
    "instagram_auth": ("instagram",),
}


def dependents_of(connection_type: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(tool_ids, channel_ids) that stop working when this connection goes away."""
    return (
        DEPENDENT_TOOLS.get(connection_type, ()),
        DEPENDENT_CHANNELS.get(connection_type, ()),
    )
