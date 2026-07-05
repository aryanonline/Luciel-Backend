"""Phone number lifecycle — BYO-only (Architecture §3.1.4 / §3.1.6, Decision #48).

Phone is opt-in via channel selection. The platform NEVER provisions, pools, or
acquires phone numbers — it is not a telephony reseller. The tenant supplies their
OWN E.164 number (BYO); we record it as an sms_sender connection
(non_secret_config.destination = the E.164 number). Until a number is supplied the
channel is NOT live and shows "Action needed: add your number" (§3.1.4).

Registration split (§3.1.6): the tenant is the Brand; the platform may operate
carrier registration (A2P 10DLC) as an ISV under the tenant's identity, and carrier
costs incl. registration are the tenant's. A supplied number is therefore recorded
as ``pending_carrier_registration`` (honest — the number is attached but carrier
registration is not yet approved) rather than ``connected``; the registration-status
poller flips it to ``connected`` on approval. Disabling does NOT release the number —
the connection row is retained so re-enabling needs no re-setup (§3.7.1 / §3.8.7 B).
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import InstanceConnection

# Basic E.164: leading '+', first digit 1-9, then 7-14 more digits (8-15 total).
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _validate_e164(e164_number: str) -> str:
    number = e164_number.strip()
    if not _E164_RE.match(number):
        raise ValueError(f"invalid E.164 phone number: {e164_number!r}")
    return number


async def attach_sms_number(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    e164_number: str,
) -> InstanceConnection:
    """Attach the tenant's BYO SMS sender number (idempotent per tenant/instance).

    Validates the E.164 format and creates (or updates, retained across disable) the
    ``sms_sender`` connection with the supplied number in
    ``non_secret_config.destination``. NO outbound provisioning call exists — the
    number is the tenant's own (BYO, §3.1.4). Status is
    ``pending_carrier_registration`` (§3.1.6) until the carrier approves.
    """
    number = _validate_e164(e164_number)
    detail = "Being activated with the carriers (A2P 10DLC) — usually a few business days."

    existing = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "sms_sender",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Reuse the retained row; update the destination to the supplied number.
        existing.provider = "twilio"
        existing.auth_class = "api_key"
        existing.non_secret_config = {"destination": number}
        existing.status = "pending_carrier_registration"
        existing.status_detail = detail
        await session.flush()
        return existing

    conn = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type="sms_sender",
        # BYO: the admin supplies the number (a tenant-owned resource), so this is an
        # api_key-class credential intake, not a platform-provisioned resource.
        auth_class="api_key",
        provider="twilio",
        status="pending_carrier_registration",
        non_secret_config={"destination": number},
        status_detail=detail,
    )
    session.add(conn)
    await session.flush()
    return conn


async def sms_number_status(
    session: AsyncSession, *, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str:
    """Report the SMS number state for the channel view (§3.1.4).

    Returns the non-revoked ``sms_sender`` connection status when a number has been
    supplied (``pending_carrier_registration`` / ``connected``), or ``unconfigured``
    when no number is attached yet — the "Action needed: add your number" state.
    """
    existing = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "sms_sender",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return "unconfigured"
    return existing.status
