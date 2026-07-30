"""Phone number lifecycle — BYO-only (Architecture §3.1.4 / §3.1.6, Decision #48).

Phone is opt-in via channel selection. The platform NEVER provisions, pools, or
acquires phone numbers — it is not a telephony reseller. The tenant supplies their
OWN E.164 number (BYO); we record it as an sms_sender connection
(non_secret_config.destination = the E.164 number). Until a number is supplied the
channel is NOT live and shows "Action needed: add your number" (§3.1.4).

OPERABILITY GATE (§3.1.4 honesty). A number that is a valid E.164 string is NOT
necessarily one our Twilio transport can send/receive on. "BYO" means the tenant's
number must be *operable by the account* — a Twilio-owned or Twilio-hosted number —
not merely "a number that exists on some carrier." So on attach we probe Twilio
(``number_operability``): operable → ``pending_carrier_registration`` (honest: the
number works with us; A2P/10DLC carrier registration is the tenant's next step);
NOT operable → ``not_operable_hosting_required`` with hosting guidance — never a
"being activated" state for a number nothing is activating. This closes the prior
silent-failure bug where any E.164 string was accepted and shown as "being activated"
with no mechanism behind it.

RE-VERIFY (user-triggered, stateless). There is no background poller. The tenant,
after hosting/registering their number, triggers ``reverify_sms_number`` which re-runs
the SAME operability probe against whatever number is *currently* attached and
transitions to operable only on a confirmed live result — repeatable forever, no
expiry, and it reads the current number (so a swapped-in number is checked, not a
stale one).

NUMBER-CHANGE INVALIDATION. Attaching/changing the number ALWAYS re-runs the gate
from scratch, so a swapped-in number can never inherit the prior number's status —
status is bound to the specific number, and changing the number re-derives it.

HONEST BOUNDARY: the operability probe confirms the number is operable by the account;
it does NOT confirm A2P 10DLC campaign approval (that Twilio endpoint is not reachable
with the deployment's API-key auth). Carrier registration remains the tenant's, stated
as such rather than falsely verified.

Disabling does NOT release the number — the connection row is retained so re-enabling
needs no re-setup (§3.7.1 / §3.8.7 B).
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.number_operability import NumberOperabilityProbe, OperabilityResult
from app.channels.tenant_twilio import build_tenant_operability_probe
from app.models.connection import InstanceConnection

# Basic E.164: leading '+', first digit 1-9, then 7-14 more digits (8-15 total).
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# --- SMS-sender status values (app-level strings; column is VARCHAR(32)) -----------
STATUS_UNCONFIGURED = "unconfigured"  # no number supplied — "add your number"
STATUS_PENDING_REGISTRATION = "pending_carrier_registration"  # operable; 10DLC is tenant's
STATUS_NOT_OPERABLE = "not_operable_hosting_required"  # E.164 valid but not in the account
STATUS_CONNECTED = "connected"  # (reserved) fully live

# Honest, mechanism-backed status details (no "being activated" fiction).
_DETAIL_PENDING = (
    "Your number is connected to Luciel. Before it can send to US recipients, complete "
    "A2P 10DLC carrier registration in your Twilio account — this is the tenant's "
    "responsibility and Luciel cannot verify campaign approval on your behalf."
)
_DETAIL_NOT_OPERABLE = (
    "This number isn't set up for programmatic messaging in your Twilio account. Host "
    "or port it into your Twilio account (Twilio Console → Phone Numbers → Hosted/Port), "
    "then click Re-verify. A number that only works on your existing carrier cannot be "
    "used until it is Twilio-operable."
)
_DETAIL_UNKNOWN = (
    "Couldn't reach Twilio to verify this number right now. It has not been marked live; "
    "click Re-verify to try again."
)


def _validate_e164(e164_number: str) -> str:
    number = e164_number.strip()
    if not _E164_RE.match(number):
        raise ValueError(f"invalid E.164 phone number: {e164_number!r}")
    return number


def _status_for(result: OperabilityResult) -> tuple[str, str]:
    """Map an operability result to (status, status_detail). NOT_OPERABLE and UNKNOWN
    both stay non-live (honest) — only OPERABLE advances to pending-registration."""
    if result is OperabilityResult.OPERABLE:
        return STATUS_PENDING_REGISTRATION, _DETAIL_PENDING
    if result is OperabilityResult.NOT_OPERABLE:
        return STATUS_NOT_OPERABLE, _DETAIL_NOT_OPERABLE
    return STATUS_NOT_OPERABLE, _DETAIL_UNKNOWN


async def _find_sms_sender(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> InstanceConnection | None:
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "sms_sender",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def attach_sms_number(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    e164_number: str,
    probe: NumberOperabilityProbe | None = None,
) -> InstanceConnection:
    """Attach (or change) the tenant's BYO SMS number, gated on live operability.

    Validates E.164 format, then probes Twilio for operability. Operable → pending
    carrier registration (honest); not operable / unknown → ``not_operable_hosting_
    required`` with guidance. NO number provisioning happens (BYO, §3.1.4). Changing
    the number re-runs the gate from scratch (number-change invalidation), so a new
    number never inherits the old number's status. Idempotent per tenant/instance.
    """
    number = _validate_e164(e164_number)
    probe = probe or await build_tenant_operability_probe(session, admin_id, instance_id)
    result = await probe.check(number)
    status, detail = _status_for(result)

    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is not None:
        existing.provider = "twilio"
        existing.auth_class = "api_key"
        # Merge, don't replace: the BYO Twilio account id (§3.1.4) also lives here, and
        # designating a number must not silently unbind the account it belongs to.
        existing.non_secret_config = {**(existing.non_secret_config or {}), "destination": number}
        existing.status = status
        existing.status_detail = detail
        await session.flush()
        return existing

    conn = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        connection_type="sms_sender",
        auth_class="api_key",
        provider="twilio",
        status=status,
        non_secret_config={"destination": number},
        status_detail=detail,
    )
    session.add(conn)
    await session.flush()
    return conn


async def reverify_sms_number(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    probe: NumberOperabilityProbe | None = None,
) -> InstanceConnection | None:
    """Re-run the operability probe against the CURRENTLY attached number (§3.1.4).

    Stateless + repeatable: reads the live ``destination`` from the connection row (so a
    swapped-in number is what gets checked, never a remembered value) and re-derives the
    status from a fresh Twilio probe. No background poller, no expiry — the tenant clicks
    this after hosting/registering their number. Returns the updated row, or None if no
    number has been attached yet (nothing to re-verify)."""
    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is None:
        return None
    destination = (existing.non_secret_config or {}).get("destination")
    if not destination:
        return None

    probe = probe or await build_tenant_operability_probe(session, admin_id, instance_id)
    result = await probe.check(str(destination))
    status, detail = _status_for(result)
    existing.status = status
    existing.status_detail = detail
    await session.flush()
    return existing


async def sms_number_status(
    session: AsyncSession, *, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str:
    """Report the SMS number state for the channel view (§3.1.4).

    Returns the non-revoked ``sms_sender`` status (``pending_carrier_registration`` /
    ``not_operable_hosting_required`` / ``connected``), or ``unconfigured`` when no
    number is attached — the "Action needed: add your number" state."""
    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is None:
        return STATUS_UNCONFIGURED
    return existing.status
