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

from dataclasses import dataclass, field

from app.config import get_settings


@dataclass(frozen=True)
class OAuthClientConfig:
    """The application's OAuth-client registration for one provider (§3.8.3: this
    is NOT the per-tenant token — it's who the app claims to be to the provider)."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class ProviderAuthorize:
    """One provider's authorize endpoint + the scopes this platform requests."""

    endpoint: str
    scopes: str
    supports_pkce: bool
    # Provider-specific params that are part of OUR request, not the user's choice
    # (e.g. Google only returns a refresh_token for an offline+consent request).
    extra: dict[str, str] = field(default_factory=dict)


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
    # §3.8.5 health cadence is keyed on auth_class; this is the behavior key the
    # connection row inherits, so a new provider in an existing class needs no code.
    auth_class: str = "oauth_token"
    # Knowledge sources: the scope the owner may select (Decision #9). None = no scope.
    scope_kind: str | None = None
    # Human-readable one-liner the UI shows next to the choice.
    help_text: str = ""

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
            scopes="https://www.googleapis.com/auth/calendar.events",
            supports_pkce=True,
            extra={"access_type": "offline", "prompt": "consent"},
        ),
        client_id_setting="google_calendar_client_id",
        client_secret_setting="google_calendar_client_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="google_calendar_redirect_uri",
        help_text="Luciel reads your availability and writes bookings into this calendar.",
    ),
    "calendly": ProviderSpec(
        provider="calendly",
        connection_type="calendar",
        display_name="Calendly",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://auth.calendly.com/oauth/authorize",
            scopes="default",
            supports_pkce=True,
        ),
        client_id_setting="calendly_client_id",
        client_secret_setting="calendly_client_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="calendly_redirect_uri",
        help_text="Luciel books, moves, and cancels through your Calendly event types.",
    ),
    # --- CRM (Decision #6: hubspot | salesforce | custom_webhook) -----------------
    "hubspot": ProviderSpec(
        provider="hubspot",
        connection_type="crm",
        display_name="HubSpot",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://app.hubspot.com/oauth/authorize",
            scopes="crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read",
            # HubSpot's authorize endpoint rejects unknown query params, and it does not
            # implement PKCE — sending a code_challenge fails the request outright.
            supports_pkce=False,
        ),
        client_id_setting="hubspot_client_id",
        client_secret_setting="hubspot_client_secret",  # noqa: S106 — field NAME
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
            scopes="api refresh_token",
            supports_pkce=True,
        ),
        client_id_setting="salesforce_consumer_key",
        client_secret_setting="salesforce_consumer_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="salesforce_redirect_uri",
        help_text="Push captured leads into Salesforce.",
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
        help_text="Look records up live from your system of record.",
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
            scopes="https://www.googleapis.com/auth/drive.readonly",
            supports_pkce=True,
            extra={
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            },
        ),
        client_id_setting="google_drive_client_id",
        client_secret_setting="google_drive_client_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="google_drive_redirect_uri",
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
            scopes="",
            supports_pkce=False,
            extra={"owner": "user"},
        ),
        client_id_setting="notion_client_id",
        client_secret_setting="notion_client_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="notion_redirect_uri",
        scope_kind=SCOPE_NOTION_PAGES,
        help_text="Choose which pages or databases Luciel may read.",
    ),
    # --- Meta channels (Decision #7) ----------------------------------------------
    # WhatsApp and Instagram/Messenger both ride the single ``channel_auth`` connection
    # (§3.1.3) — they bind to a WhatsApp phone-number id / IG-account or Page id, not to
    # a phone number of ours. Both are Meta OAuth against the same Graph app, differing
    # only in the scopes requested, which is exactly what a registry entry expresses.
    "meta_whatsapp": ProviderSpec(
        provider="meta_whatsapp",
        connection_type="channel_auth",
        display_name="WhatsApp Business",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://www.facebook.com/v19.0/dialog/oauth",
            scopes="whatsapp_business_messaging,whatsapp_business_management,business_management",
            # Meta's dialog/oauth does not implement PKCE; state carries the CSRF proof.
            supports_pkce=False,
        ),
        client_id_setting="meta_app_id",
        client_secret_setting="meta_app_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="meta_redirect_uri",
        help_text="Reply on WhatsApp from your WhatsApp Business number.",
    ),
    "meta_instagram": ProviderSpec(
        provider="meta_instagram",
        connection_type="channel_auth",
        display_name="Instagram DM & Messenger",
        auth_kind=AUTH_OAUTH,
        authorize=ProviderAuthorize(
            endpoint="https://www.facebook.com/v19.0/dialog/oauth",
            scopes="instagram_basic,instagram_manage_messages,pages_messaging,"
            "pages_show_list,business_management",
            supports_pkce=False,
        ),
        client_id_setting="meta_app_id",
        client_secret_setting="meta_app_secret",  # noqa: S106 — field NAME
        redirect_uri_setting="meta_redirect_uri",
        help_text="Reply to Instagram DMs and Facebook Messenger from your Page.",
    ),
    # --- Senders (provisioned / platform-owned, no owner-facing choice) -----------
    "ses": ProviderSpec(
        provider="ses",
        connection_type="email_sender",
        display_name="Luciel work email",
        auth_kind=AUTH_PROVISIONED,
        auth_class="api_key",
        help_text="The address Luciel sends and receives on.",
    ),
    "twilio": ProviderSpec(
        provider="twilio",
        connection_type="sms_sender",
        display_name="Your own phone number",
        auth_kind=AUTH_PROVISIONED,
        auth_class="api_key",
        help_text="Bring your own number; Luciel texts from it.",
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
    "channel_auth": ("meta_whatsapp", "meta_instagram"),
}


# The connection types that present the owner a provider choice (Decision #6).
CHOICE_TYPES: frozenset[str] = frozenset(_ORDER)

# Providers ACCEPTED for a connection type but deliberately absent from its owner-facing
# choice list. Knowledge sync (§3.2.3) rides ``knowledge_source`` connections for two cases
# that are not "pick a knowledge provider" options: the CRM knowledge base (same vendor
# grant as the CRM connection, reached via the crm_kb origin) and website_crawl, which has
# no external account at all — it is an httpx fetch target. Accepting them here keeps the
# validation gate honest without inventing UI choices that do not exist.
_ALSO_ACCEPTED: dict[str, frozenset[str]] = {
    "knowledge_source": frozenset({"hubspot", "salesforce", "crm_kb", "website_crawl"}),
}


def get_provider(provider: str) -> ProviderSpec | None:
    """The spec for a provider id, or None if it is not registered."""
    return PROVIDERS.get(provider)


def providers_for(connection_type: str) -> list[ProviderSpec]:
    """The selectable providers for a connection type, in owner-facing display order.

    Types absent from ``_ORDER`` (the provisioned senders) fall back to whatever is
    registered for them, so the function is total over the catalog."""
    ordered = _ORDER.get(connection_type)
    if ordered is not None:
        return [PROVIDERS[p] for p in ordered if p in PROVIDERS]
    return [s for s in PROVIDERS.values() if s.connection_type == connection_type]


def default_provider(connection_type: str) -> str | None:
    """The provider used when the caller does not choose one (first in display order)."""
    options = providers_for(connection_type)
    return options[0].provider if options else None


def has_choice(connection_type: str) -> bool:
    """True iff this type presents the owner a PROVIDER CHOICE (Decision #6).

    Only these types are validated against the registry on connect. The provisioned
    senders (email/SMS) have no owner-facing choice — their ``provider`` column is just a
    brand label for the platform-owned integration behind them, so pinning it to a
    registry entry would reject labels already in the field for no safety gain. Where a
    choice EXISTS the string steers a real OAuth endpoint, so it is validated."""
    return connection_type in _ORDER


def is_valid_provider(connection_type: str, provider: str) -> bool:
    """True iff ``provider`` is registered FOR this connection type.

    Gate every provider selection through this: it is what stops an admin-supplied
    string from steering a connect flow at an arbitrary endpoint."""
    if provider in _ALSO_ACCEPTED.get(connection_type, frozenset()):
        return True
    spec = PROVIDERS.get(provider)
    return spec is not None and spec.connection_type == connection_type


def oauth_client_config(provider: str) -> OAuthClientConfig:
    """Read a provider's OAuth app registration from settings via the registry.

    The registry stores settings FIELD NAMES, so this resolves any registered provider
    without a per-provider branch — that is what makes adding a provider config rather
    than code. Unregistered/non-OAuth providers return an unconfigured (empty) config
    rather than raising, so the catalog can grow and a live flow fails closed."""
    spec = PROVIDERS.get(provider)
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

    return OAuthClientConfig(
        _read(spec.client_id_setting),
        _read(spec.client_secret_setting),
        _read(spec.redirect_uri_setting),
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
    "channel_auth": ("whatsapp", "instagram"),
}


def dependents_of(connection_type: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(tool_ids, channel_ids) that stop working when this connection goes away."""
    return (
        DEPENDENT_TOOLS.get(connection_type, ()),
        DEPENDENT_CHANNELS.get(connection_type, ()),
    )
