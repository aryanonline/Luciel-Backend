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

from app.schemas_api.common import WireModel

EmailProvisioningModeWire = Literal["own_domain", "vm_subdomain"]


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

    mode: EmailProvisioningModeWire
    email_address: str
    status: str
    # own_domain only: records the admin must add to complete inbound routing.
    dns_records: list[DnsRecord] | None = None
