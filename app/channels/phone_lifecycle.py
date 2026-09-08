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
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.number_operability import NumberOperabilityProbe, OperabilityResult
from app.channels.tenant_twilio import build_tenant_operability_probe
from app.models.connection import InstanceConnection

if TYPE_CHECKING:
    from app.channels.tenant_twilio import TwilioWebhookConfigurator


class NumberAlreadyClaimed(Exception):
    """The number is another tenant's inbound-routing destination.

    Same security boundary as email/Meta destination binds: the destination is what
    ``channel_resolve_destination`` routes inbound SMS *and voice calls* on, so a
    tenant permitted to claim a number another tenant already holds could capture
    their inbound conversations.
    """

    def __init__(self, number: str) -> None:
        super().__init__(f"{number} is already connected to a different account")


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
# Round 5B item 10: the honest exit from pending_carrier_registration. The A2P
# status API is unreachable with our auth (number_operability.py), so the OWNER'S
# audited attestation — not a fabricated verification — is what turns texting on.
_DETAIL_ATTESTED = (
    "Texting is enabled on your attestation that your A2P 10DLC carrier registration "
    "is approved (or does not apply to your recipients). VantageMind cannot verify "
    "campaign approval on your behalf. Re-verify still checks that the number remains "
    "operable on your account."
)
# non_secret_config keys (JSONB, non-secret §3.8.3) the attestation writes:
#   carrier_registration_attested_at: ISO timestamp
#   carrier_registration_attested_by: str(user_id) or "" when unknown
_ATTESTED_AT_KEY = "carrier_registration_attested_at"
_ATTESTED_BY_KEY = "carrier_registration_attested_by"


class AttestationNotReady(ValueError):
    """Attestation preconditions unmet: no confirmed number, or a row whose
    credential/operability problem must be repaired first."""


def _validate_e164(e164_number: str) -> str:
    number = e164_number.strip()
    if not _E164_RE.match(number):
        raise ValueError(f"invalid E.164 phone number: {e164_number!r}")
    return number


def _status_for(result: OperabilityResult, cfg: dict[str, object] | None = None) -> tuple[str, str]:
    """Map an operability result to (status, status_detail). NOT_OPERABLE and UNKNOWN
    both stay non-live (honest) — only OPERABLE advances, to pending-registration
    or, when the owner has attested their carrier registration (round 5B item 10),
    to connected. An inoperable number outranks any attestation."""
    if result is OperabilityResult.OPERABLE:
        if cfg and cfg.get(_ATTESTED_AT_KEY):
            return STATUS_CONNECTED, _DETAIL_ATTESTED
        return STATUS_PENDING_REGISTRATION, _DETAIL_PENDING
    if result is OperabilityResult.NOT_OPERABLE:
        return STATUS_NOT_OPERABLE, _DETAIL_NOT_OPERABLE
    return STATUS_NOT_OPERABLE, _DETAIL_UNKNOWN


def status_for_confirmed_number(cfg: dict[str, object]) -> tuple[str, str]:
    """(status, detail) for an sms row whose ``destination`` is already the
    confirmed-operable claim: connected iff attested, else pending (§3.1.4).

    Used by the credential/reconnect paths, which have no fresh probe in hand —
    they must not blanket-promote to connected past the registration gate (round
    5B: the sweep's promotion bug used to do exactly that, and submit_credentials
    had the same blanket ``connected``)."""
    if cfg.get(_ATTESTED_AT_KEY):
        return STATUS_CONNECTED, _DETAIL_ATTESTED
    return STATUS_PENDING_REGISTRATION, _DETAIL_PENDING


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
    webhook_config: TwilioWebhookConfigurator | None = None,
) -> InstanceConnection:
    """Attach (or change) the tenant's BYO SMS number, gated on live operability.

    Validates E.164 format, then probes Twilio for operability. Operable → pending
    carrier registration (honest); not operable / unknown → ``not_operable_hosting_
    required`` with guidance. NO number provisioning happens (BYO, §3.1.4). Changing
    the number re-runs the gate from scratch (number-change invalidation), so a new
    number never inherits the old number's status. Idempotent per tenant/instance.
    """
    number = _validate_e164(e164_number)
    # Cross-tenant uniqueness (§3.1.3) BEFORE the operability probe: every other
    # destination bind (email, Meta) runs this gate; this was the one path without
    # it, which let a second tenant claim a number inbound routing already resolved
    # to someone else. Checked through the same SECURITY DEFINER resolver delivery
    # uses, because RLS hides foreign claims from an ordinary query.
    from app.channels import routing  # noqa: PLC0415 — circular at module import

    # TOCTOU guard (c21): serialize concurrent claims on this number for the
    # transaction, so two tenants racing the check below cannot both bind.
    await routing.lock_destination_claim(session, number)
    holder = await routing.owner_of_destination(session, "sms_sender", number)
    if holder is not None and holder != admin_id:
        raise NumberAlreadyClaimed(number)
    probe = probe or await build_tenant_operability_probe(session, admin_id, instance_id)
    result = await probe.check(number)

    # Anti-squatting (c21): the routable CLAIM (``destination``) is written only on
    # a CONFIRMED-operable probe — a number the account cannot operate must not
    # block the real owner's claim. A failed/unknown probe stores the number under
    # ``pending_destination`` instead; a later confirmed re-verify PROMOTES it
    # (re-running the ownership gate — promotion is a claim point too).
    if result is OperabilityResult.OPERABLE:
        cfg_update: dict[str, object] = {"destination": number}
        cfg_pop = "pending_destination"
    else:
        cfg_update = {"pending_destination": number}
        cfg_pop = "destination"

    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is not None:
        existing.provider = "twilio"
        existing.auth_class = "api_key"
        # Merge, don't replace: the BYO Twilio account id (§3.1.4) also lives here, and
        # designating a number must not silently unbind the account it belongs to.
        prior_cfg = existing.non_secret_config or {}
        merged = {**prior_cfg, **cfg_update}
        merged.pop(cfg_pop, None)
        # Number-change invalidation extends to the attestation (round 5B item
        # 10): the owner attested THAT number's registration, so a different
        # number re-derives from scratch. A same-number re-attach keeps it.
        prior_number = prior_cfg.get("destination") or prior_cfg.get("pending_destination")
        if prior_number and prior_number != number:
            merged.pop(_ATTESTED_AT_KEY, None)
            merged.pop(_ATTESTED_BY_KEY, None)
        status, detail = _status_for(result, merged)
        existing.non_secret_config = merged
        existing.status = status
        existing.status_detail = detail
        conn = existing
    else:
        status, detail = _status_for(result, dict(cfg_update))
        conn = InstanceConnection(
            connection_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type="sms_sender",
            auth_class="api_key",
            provider="twilio",
            status=status,
            non_secret_config=dict(cfg_update),
            status_detail=detail,
        )
        session.add(conn)
    await session.flush()

    # Webhook auto-configuration (c20, live-caught 2026-08-18: nothing — code or
    # copy — ever pointed the number's webhooks at the platform, so a designated
    # number answered nothing until someone edited the Twilio Console by hand, and
    # a number CHANGE silently killed both channels). Best-effort AFTER the row is
    # written: a configuration failure NEVER fails the attach — the honest degraded
    # state is the manual guidance appended to status_detail. Only an OPERABLE
    # number is configured (a number not in the account has nothing to configure).
    if result is OperabilityResult.OPERABLE:
        await configure_number_webhooks(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            conn=conn,
            e164_number=number,
            webhook_config=webhook_config,
        )
    return conn


async def configure_number_webhooks(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    conn: InstanceConnection,
    e164_number: str,
    webhook_config: TwilioWebhookConfigurator | None = None,
    audit_on_success: bool = True,
) -> bool:
    """Point the number's Voice/SMS/StatusCallback webhooks at the platform.

    Shared by the designate path (attach_sms_number) and the credentials path
    (submit_credentials, for the credentials-after-number order). Best-effort:
    False appends manual guidance to ``status_detail`` and audits the failure —
    never raises, never blocks the flow that called it."""
    from app.audit import log as audit_log  # noqa: PLC0415
    from app.channels.tenant_twilio import (  # noqa: PLC0415
        build_tenant_webhook_configurator,
        build_webhook_urls,
        ensure_capability_token,
    )
    from app.config import get_settings  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    plain_urls = build_webhook_urls(get_settings().public_api_base_url)
    if plain_urls is None:
        # No public API origin configured (mock/CI/local): auto-config is not part
        # of this deployment at all — a silent no-op, never a "failure" note.
        return False
    # C11: a tenant whose Account Auth Token we do not hold (OAuth grant,
    # API-key-only form) can never pass X-Twilio-Signature verification — their
    # registered URLs carry a per-connection capability token instead. Minted
    # once, before URL construction; None for signature-covered tenants, whose
    # URLs stay byte-identical.
    capability, minted = await ensure_capability_token(conn)
    if minted:
        await audit_log.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="webhook_capability_provisioned",
            payload={"connection_type": "sms_sender", "number": e164_number},
        )
    urls = (
        build_webhook_urls(get_settings().public_api_base_url, capability_token=capability)
        or plain_urls
    )
    configurator = webhook_config or await build_tenant_webhook_configurator(
        session, admin_id, instance_id
    )
    applied = await configurator.configure(e164_number, urls)
    if applied and not audit_on_success:
        # The periodic drift re-apply (round 5B item 15) is an idempotent
        # re-point every sweep — auditing each quiet success would spam the
        # customer's audit page 4×/day. Failures always audit below.
        await session.flush()
        return True
    if applied:
        await audit_log.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="webhook_autoconfig_applied",
            payload={"connection_type": "sms_sender", "number": e164_number},
        )
    else:
        manual = (
            " We couldn't point your number at Luciel automatically. In your Twilio "
            f"Console, set the number's Voice webhook to {urls.voice_url}, "
            f"Messaging to {urls.sms_url}, and the call status callback to "
            f"{urls.status_callback_url}."
        )
        # Idempotent append: the periodic re-apply (round 5B item 15) runs every
        # sweep — a persistently failing configurator must not grow the detail
        # without bound.
        if manual.strip() not in (conn.status_detail or ""):
            conn.status_detail = (conn.status_detail or "").rstrip() + manual
        await audit_log.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="webhook_autoconfig_failed",
            payload={"connection_type": "sms_sender", "number": e164_number},
        )
    await session.flush()
    return applied


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
    cfg = existing.non_secret_config or {}
    destination = cfg.get("destination") or cfg.get("pending_destination")
    if not destination:
        return None

    probe = probe or await build_tenant_operability_probe(session, admin_id, instance_id)
    result = await probe.check(str(destination))

    # Promotion (c21 anti-squatting): a pending number that just probed OPERABLE
    # becomes the routable claim — a NEW claim point, so it re-runs the ownership
    # gate under the same per-destination advisory lock as attach. A failed probe
    # never strips an EXISTING claim (a transient Twilio outage must not de-route
    # a working number); it only keeps a pending number pending.
    if result is OperabilityResult.OPERABLE and not cfg.get("destination"):
        from app.channels import routing  # noqa: PLC0415 — circular at module import

        await routing.lock_destination_claim(session, str(destination))
        holder = await routing.owner_of_destination(session, "sms_sender", str(destination))
        if holder is not None and holder != admin_id:
            raise NumberAlreadyClaimed(str(destination))
        merged = {**cfg, "destination": destination}
        merged.pop("pending_destination", None)
        existing.non_secret_config = merged

    # Post-promotion cfg: an attested row that re-probes OPERABLE stays connected;
    # a failed probe demotes honestly but RETAINS the attestation keys (a Twilio
    # outage must not erase the owner's attestation — round 5B item 10).
    status, detail = _status_for(result, existing.non_secret_config or {})
    existing.status = status
    existing.status_detail = detail
    await session.flush()

    # Round 5B item 15 — webhook drift repair: Re-verify is the customer's "make
    # my number work again" action, so it also re-points the number's webhooks at
    # the platform (best-effort, same configurator as attach). A console edit or
    # a platform base-URL change used to kill inbound with the row still reading
    # Connected and NOTHING re-checking the number's config, ever.
    if result is OperabilityResult.OPERABLE:
        await configure_number_webhooks(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            conn=existing,
            e164_number=str(destination),
        )
    return existing


async def attest_carrier_registration(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> InstanceConnection | None:
    """Owner attests their A2P 10DLC registration is approved — or does not apply
    to their recipients (round 5B item 10).

    The A2P status API is unreachable with our auth (number_operability.py), so
    the owner's AUDITED word is the honest exit from pending_carrier_registration
    — consistent with BYO doctrine: the customer is the registered sender of
    record; the platform guides and verifies operability, never registration.

    Preconditions: a confirmed ``destination`` (operability proven at claim time)
    AND status in (pending_carrier_registration, connected) — an error/expired
    row must repair credentials first; a not-operable row must re-verify first.
    Returns None when no sms row exists; raises AttestationNotReady on unmet
    preconditions. Idempotent when already attested."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.audit import log as audit_log  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is None:
        return None
    cfg = dict(existing.non_secret_config or {})
    number = cfg.get("destination")
    if not number:
        raise AttestationNotReady(
            "Add and verify your number first — attestation applies to a working number."
        )
    if existing.status not in (STATUS_PENDING_REGISTRATION, STATUS_CONNECTED):
        raise AttestationNotReady(
            "This connection needs repair first (re-verify the number or fix its "
            "credentials), then attest."
        )
    if cfg.get(_ATTESTED_AT_KEY):
        return existing  # idempotent — the first attestation stands
    cfg[_ATTESTED_AT_KEY] = datetime.now(UTC).isoformat()
    cfg[_ATTESTED_BY_KEY] = str(user_id) if user_id else ""
    existing.non_secret_config = cfg
    old_status = existing.status
    existing.status, existing.status_detail = status_for_confirmed_number(cfg)
    await audit_log.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="carrier_registration_attested",
        payload={
            "connection_id": str(existing.connection_id),
            "number": str(number),
            "attested_by": str(user_id) if user_id else "",
        },
    )
    if existing.status != old_status:
        await audit_log.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(existing.connection_id),
                "old_status": old_status,
                "new_status": existing.status,
                "reason": "carrier_registration_attested",
            },
        )
    await session.flush()
    return existing


class NoNumberToRemove(Exception):
    """The sms row holds no designated (or pending) number to remove."""


async def detach_sms_number(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    user_id: uuid.UUID | None,
    webhook_config: TwilioWebhookConfigurator | None = None,
) -> InstanceConnection | None:
    """Remove the designated number and keep the Twilio account (round 6 WP-D).

    The owner's own number is their asset; taking it off the Luciel must not cost
    them the account credentials or make them re-consent. The routable claim
    (``destination`` / ``pending_destination``) and the number-bound attestation
    are dropped, the row returns to the honest "add your number" state the
    credential path uses, and — best effort, never blocking — the number's
    webhooks are pointed away from the platform so a number the owner reuses
    elsewhere does not keep ringing here. Returns None when no sms row exists;
    raises NoNumberToRemove when nothing is on file."""
    from app.audit import log as audit_log  # noqa: PLC0415
    from app.connections.service import SMS_NEEDS_NUMBER_DETAIL  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is None:
        return None
    cfg = dict(existing.non_secret_config or {})
    number = cfg.get("destination") or cfg.get("pending_destination")
    if not number:
        raise NoNumberToRemove()
    for key in ("destination", "pending_destination", _ATTESTED_AT_KEY, _ATTESTED_BY_KEY):
        cfg.pop(key, None)
    old_status = existing.status
    existing.non_secret_config = cfg
    existing.status = "unconfigured"
    existing.status_detail = SMS_NEEDS_NUMBER_DETAIL
    existing.last_health_check_at = None
    await session.flush()
    unpointed = False
    if isinstance(number, str) and cfg.get("accountSid"):
        try:
            from app.channels.tenant_twilio import (  # noqa: PLC0415
                WebhookUrls,
                build_tenant_webhook_configurator,
            )

            configurator = webhook_config or await build_tenant_webhook_configurator(
                session, admin_id, instance_id
            )
            unpointed = await configurator.configure(
                number, WebhookUrls(voice_url="", sms_url="", status_callback_url="")
            )
        except Exception:  # noqa: BLE001 — best effort; the audit row says what happened
            unpointed = False
    await audit_log.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="connection_status_changed",
        payload={
            "connection_id": str(existing.connection_id),
            "old_status": old_status,
            "new_status": existing.status,
            "reason": "sms_number_removed",
            "number": str(number),
            "webhooks_unpointed": unpointed,
        },
    )
    return existing


async def withdraw_carrier_attestation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> InstanceConnection | None:
    """Take back the owner's carrier-registration attestation (round 6 WP-D).

    Texting returns to the honest ``pending_carrier_registration`` gate; the
    number, credentials and calls are untouched. Idempotent when nothing was
    attested. Returns None when no sms row exists."""
    from app.audit import log as audit_log  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    existing = await _find_sms_sender(session, admin_id, instance_id)
    if existing is None:
        return None
    cfg = dict(existing.non_secret_config or {})
    if not cfg.get(_ATTESTED_AT_KEY):
        return existing
    cfg.pop(_ATTESTED_AT_KEY, None)
    cfg.pop(_ATTESTED_BY_KEY, None)
    existing.non_secret_config = cfg
    old_status = existing.status
    if cfg.get("destination") and existing.status == STATUS_CONNECTED:
        existing.status, existing.status_detail = status_for_confirmed_number(cfg)
    await audit_log.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="carrier_registration_attestation_withdrawn",
        payload={"connection_id": str(existing.connection_id)},
    )
    if existing.status != old_status:
        await audit_log.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(existing.connection_id),
                "old_status": old_status,
                "new_status": existing.status,
                "reason": "carrier_registration_attestation_withdrawn",
            },
        )
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
