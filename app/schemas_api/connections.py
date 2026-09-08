"""Wire models for the admin Connections API (Architecture §3.8, §3.8.3).

These mirror the frozen Zod contract (packages/api-client/src/schemas/connections.ts)
EXACTLY: Connection + StartConnectionResult, camelCase on the wire via WireModel.

§3.8.3 secret hygiene — HARD: ConnectionOut exposes provider + nonSecretConfig +
status ONLY. There is deliberately NO field on this model that can carry secret
material (``secret_ref``, tokens, credentials all stay server-side). The export and
UI therefore physically CANNOT leak a secret. A dedicated test asserts this.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.schemas_api.common import WireModel

EmailProvisioningModeWire = Literal["own_domain", "vm_subdomain"]
# The OUT view carries one more mode than the request accepts, deliberately:
# ``byo_mailbox`` is REPORTED (the Outlook mailbox connected via the OAuth flow is
# the sender, §3.1.6a) but never PROVISIONED here — a POST cannot ask for it, because
# a mailbox arrives through connect/verify/bind, not through this endpoint. Missing
# this split made the read 500 the moment the first real mailbox connected: the
# connect succeeded, and the panel that should have shown it crashed instead.
EmailProvisioningModeOutWire = Literal["own_domain", "vm_subdomain", "byo_mailbox"]

# The three channels one Meta connection serves (§3.1.4/§3.8.1). Each designates its own
# destination on that single connection — they are not separate connections.
MetaChannelWire = Literal["whatsapp", "instagram", "messenger"]


class TenantNumberOut(WireModel):
    """One number in the tenant's own Twilio account (the designate picker, C9)."""

    phone_number: str
    friendly_name: str


class TenantNumbersOut(WireModel):
    """The picker payload. ``numbers`` is None when listing was not possible —
    the UI falls back to manual E.164 entry rather than blocking the designate."""

    numbers: list[TenantNumberOut] | None


class CalendlyEventTypeOut(WireModel):
    """One of the owner's active Calendly event types (round 6 WP-H)."""

    uri: str
    name: str
    active: bool
    duration_minutes: int
    scheduling_url: str


class CalendlyEventTypesOut(WireModel):
    """``eventTypes`` is None when the account could not be read (token unusable)."""

    event_types: list[CalendlyEventTypeOut] | None
    chosen_uri: str | None = None


class UpdateConnectionSettingsRequest(WireModel):
    """PUT /connections/{id}/settings — provider settings that are not credentials.
    Today: the Calendly event type customers book (round 6 WP-H)."""

    model_config = ConfigDict(extra="forbid")

    event_type_uri: str | None = None
    event_type_name: str | None = None


class ConnectionOut(WireModel):
    """Non-secret view of one instance_connections row (§3.8.3). NO secret field."""

    connection_id: uuid.UUID
    connection_type: str
    provider: str
    status: str
    non_secret_config: dict[str, object] | None = None
    status_detail: str | None = None
    last_health_check_at: datetime | None = None
    created_at: datetime
    # Harmony wave 2 (item 3): True iff this provider can actually be (re)connected
    # today per app.connections.registry.provider_available -- the SAME truth the
    # Configure page (/connections/providers -> ProviderOptionOut.configured) uses.
    # A stale row for a provider that is unconfigured-in-registry (dropped from the
    # catalog, or still catalogued but its OAuth client was never registered) must
    # render "not available yet" here too, never "Action needed" -- "Action needed"
    # implies the owner can fix this by acting, which is false when the platform
    # itself has not made the provider connectable yet.
    provider_available: bool = True
    # Harmony wave 2 (item 4): the Overview row never carried a human label at all --
    # only the raw ``provider`` code (e.g. "ses"). With nothing else to render, the
    # frontend was left to title-case the raw code itself, producing "Ses" instead of
    # "Amazon SES". displayName is the SAME catalog string /connections/providers
    # already serves for Configure (registry.get_provider(provider).display_name),
    # so Overview never needs to improvise a label from a raw code again. Falls back
    # to the raw provider string only for a provider absent from the registry
    # entirely (dropped from the catalog) so the field is never empty.
    display_name: str = ""


class StartConnectionRequest(WireModel):
    """POST body to begin a connect flow: {connectionType, provider} (frozen).

    ``phoneNumber`` is an ADDITIVE optional field carrying the tenant's BYO E.164
    SMS number (§3.1.4): the platform never provisions a number, so for an
    ``sms_sender`` connect the admin supplies their own. It is non-secret config (a
    destination), NOT a credential — secret hygiene is preserved (§3.8.3)."""

    connection_type: str
    provider: str
    phone_number: str | None = None


class StartConnectionResult(WireModel):
    """Result of starting/reconnecting (frozen): authorizeUrl for OAuth-class
    providers; requiresClientForm:true for non-OAuth (CSV upload, webhook URL)."""

    authorize_url: str | None = None
    requires_client_form: bool | None = None


class SubmitCredentialsRequest(WireModel):
    """POST body completing a ``requiresClientForm`` connect: the form's field values.

    Keys are the ``credentialFields[].name`` values the providers endpoint advertised for
    the connection's provider. This is the ONE surface that accepts secret material, and
    it is write-only: fields the registry marks ``secret`` go straight to the SecretStore
    and are never echoed back on any response (§3.8.3)."""

    fields: dict[str, str]


class SwapConnectionRequest(WireModel):
    """POST body for "Change connected account" (§3.8.7 rule B). Swap re-initiates the
    connect flow against a (possibly different) provider while the CURRENT connection
    stays live until the new credential verifies (proven-before-cutover)."""

    provider: str


class SwitchConnectionRequest(WireModel):
    """POST body for "Use a different account" (Decision #5c). ``provider`` may be
    omitted to switch accounts within the same provider; when given it must be one the
    registry offers for this connection type (Decision #6)."""

    provider: str | None = None


class BindDestinationRequest(WireModel):
    """PUT body naming WHICH Meta asset this Luciel answers on (Decision #7).

    An OAuth token proves the owner authorized us; it does not say which of their
    WhatsApp numbers or Pages this Luciel serves. Inbound routing resolves a tenant by
    the destination alone, so until this is bound a `connected` Meta channel cannot
    receive a single message. This is a destination identifier (phone_number_id / Page
    id), NOT a credential — no secret hygiene concern (§3.8.3).

    ``channel`` names WHICH of the three Meta channels the asset belongs to
    (``whatsapp`` = WhatsApp-eligible number id, ``instagram`` = the IG-linked Page id,
    ``messenger`` = the Page id). One Meta grant serves all three, so they are stored
    side by side and binding one never clears another. Omitting it writes the single
    legacy destination, which is still what the SMS/email senders use.
    """

    destination: str = Field(min_length=1, max_length=128)
    channel: MetaChannelWire | None = None


class DisconnectResult(WireModel):
    """What a disconnect actually did (Decision #5b), so the UI can say so plainly.

    ``disabledTools`` / ``disabledChannels`` are the dependents switched off as a
    consequence — an owner disconnecting the calendar should be TOLD that scheduling went
    with it, not discover it later from a booking that fails."""

    connection: ConnectionOut
    secret_deleted: bool
    disabled_tools: list[str]
    disabled_channels: list[str]


class ProviderCredentialFieldOut(WireModel):
    """One field a non-OAuth provider's connect form must collect."""

    name: str
    label: str
    secret: bool
    required: bool


class ProviderOptionOut(WireModel):
    """One selectable provider for a connection type (Decision #6).

    The UI renders the choice from this list instead of hardcoding a vendor. NO secret
    material: ``configured`` is a boolean derived from whether the platform's OAuth app
    credential is present — never the credential itself (§3.8.3)."""

    provider: str
    display_name: str
    auth_kind: str
    help_text: str
    configured: bool
    credential_fields: list[ProviderCredentialFieldOut]
    # 'drive_folders' | 'notion_pages' | null — whether the owner may narrow this
    # connection's knowledge scope after connecting (Decision #9).
    scope_kind: str | None = None


class ConnectionProvidersOut(WireModel):
    """The provider choices for one connection type, in display order."""

    connection_type: str
    providers: list[ProviderOptionOut]


# --- Email provisioning (§3.1.6a / Decision #49) ----------------------------------- #
# The tenant PROVISIONS the address Luciel sends + receives on. Two modes, both LAUNCH
# capabilities: own_domain (the business's own address; requires inbound DNS/MX routing)
# and vm_subdomain (a zero-DNS VantageMind-subdomain fallback, live immediately). This
# wire shape matches Luciel-Frontend PR #13 (connections.ts) field-for-field.


class DnsRecord(WireModel):
    """One DNS record the admin must add to complete own-domain inbound routing.
    Non-secret. Empty for vm_subdomain mode."""

    type: str  # 'MX' | 'TXT' | 'CNAME'
    host: str
    value: str
    priority: int | None = None


class ProvisionEmailRequest(WireModel):
    """POST body for /connections/email: choose a mode and (own_domain) the address."""

    mode: EmailProvisioningModeWire
    # Required for own_domain: the address Luciel should send + receive on.
    email_address: str | None = None


class EmailProvisioningOut(WireModel):
    """Non-secret view of the email_sender provisioning state (§3.8.3). NO secret."""

    mode: EmailProvisioningModeOutWire
    email_address: str
    status: str
    # own_domain only: records the admin must add to complete inbound routing.
    dns_records: list[DnsRecord] | None = None


class RecordSourceCsvOut(WireModel):
    """Result of a record-source CSV upload: how many live rows now back
    ``lookup_record``. Upload REPLACES the tenant's rows (re-upload to refresh,
    Vision §3.2 'CSV upload' provider); the row count is echoed so the owner
    sees exactly what the tool now serves."""

    records: int
    columns: list[str]
