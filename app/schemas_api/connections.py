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

from app.schemas_api.common import WireModel


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
