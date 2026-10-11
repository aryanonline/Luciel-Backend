"""SMS escalation notifier (Architecture §3.5.1) — Twilio.

Same vendor as customer SMS, and it sends FROM the tenant's own provisioned number
(§3.1.4 BYO) — an owner being paged about their own business should see their own number,
and the platform has no other number to send from.

Requires the SMS channel to be provisioned. If it is not, that is reported as an
actionable failure rather than a silent drop, so the routing layer can fall back to the
owner's email (§3.5.4) instead of leaving the escalation undelivered.

Round 7 WP-4b: Twilio accepting a request is not the contact hearing. A page sent
through a live sender asks Twilio for a delivery receipt (``status_callback`` →
``/api/v1/webhooks/twilio/notifications/status``) and reports ``detail="dlr_pending"``,
so the dispatcher stamps the row ``sent`` and lets the carrier's word make it
``delivered`` or ``failed``.

Round 7 delivery hardening: that holds for BOTH credential shapes — the SDK sender
(auth token / API key) and the bearer sender of a one-click OAuth tenant, which used
to send with no receipt requested and was therefore stamped ``delivered`` on API
accept. And every accepted send through a real provider sender is ``dlr_pending``,
whether or not a callback could be requested (no public API origin, or a tenant whose
receipts could never be verified): ``delivered`` is the carrier's word — the receipt,
or the deferred sweep's one status fetch for a page whose receipt never came — never
the API's. Only the mock/Null sender reports what it always did (``recorded``).
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryReceipt
from app.channels.tenant_twilio import build_tenant_sms_sender
from app.channels.twilio_bearer import TwilioBearerSmsSender
from app.channels.twilio_sender import NullSmsSender, TwilioSmsSender
from app.config import get_settings
from app.connections.service import resolve_sms_number
from app.notifications.base import EscalationNotification, owner_facing_reasons

# One SMS segment is 160 GSM-7 chars; the body is condensed to stay in a small number of
# segments (§3.5.1 "SMS condensed form") while keeping the contact info, which is the
# whole point of paging someone.
_MAX_INTENT = 80

#: ``DeliveryReceipt.detail`` for a send a real provider sender accepted: the carrier's
#: word (a delivery receipt, or the sweep's status fetch) is still to come.
DLR_PENDING = "dlr_pending"
NOTIFICATION_STATUS_PATH = "/api/v1/webhooks/twilio/notifications/status"


async def notification_status_callback_url(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str | None:
    """Where Twilio should post this tenant's notification delivery receipts, or None
    when the deployment has no public API origin (mock/CI). Tenants whose auth token
    the platform never holds (C11: the one-click OAuth grant, API-key-only forms) get
    their capability token on the URL, exactly as the auto-configured inbound webhooks
    do, so the receipt verifies — Twilio signs a status callback with the ACCOUNT auth
    token however the send was authenticated, and that token is exactly what those
    tenants never gave us.

    None, too, for a tenant we hold NEITHER proof for (no auth token and no capability
    token minted yet): every receipt would be a 403 here and an error in the customer's
    own Twilio console, and accepting it unverified is not an option. Their page stays
    ``sent`` until the deferred sweep asks Twilio for the message's status."""
    from urllib.parse import quote  # noqa: PLC0415

    from app.channels.tenant_twilio import tenant_inbound_verification  # noqa: PLC0415

    base = (get_settings().public_api_base_url or "").rstrip("/")
    if not base:
        return None
    auth_token, capability = await tenant_inbound_verification(session, admin_id, instance_id)
    if not auth_token and not capability:
        return None
    suffix = f"?cap={quote(capability, safe='')}" if capability else ""
    return f"{base}{NOTIFICATION_STATUS_PATH}{suffix}"


class SmsNotifier:
    channel_id = "sms"

    def verify_config(self, to: str) -> bool:
        # E.164: leading + and 2-15 digits. Twilio is the real validator; this only
        # catches a contact that could never be a number.
        digits = to[1:] if to.startswith("+") else ""
        return to.startswith("+") and digits.isdigit() and 2 <= len(digits) <= 15

    def format(self, notification: EscalationNotification) -> str:
        """Condensed §3.5.1 payload: why, who to reach, what to do, which conversation."""
        if notification.safety_label:
            # Round 7 WP-4b (item 4): a moderation take-over is a safety page, never
            # "the customer asked to speak to a person".
            parts = [f"{notification.business_name}: URGENT SAFETY — {notification.safety_label}."]
        elif notification.signal == "content_flagged" and notification.reasons:
            parts = [
                f"{notification.business_name}: Luciel escalated "
                f"(content_flagged: {notification.reasons[0][:_MAX_INTENT].rstrip()})."
            ]
        else:
            parts = [f"{notification.business_name}: Luciel escalated ({notification.signal})."]
        contact = notification.lead_contact or notification.customer_identifier
        # Item 3: what the lead volunteered on top of the transport identity.
        extra = [
            v for v in (notification.lead_email, notification.lead_phone) if v and v != contact
        ]
        if notification.lead_name or contact or extra:
            who = " ".join(p for p in (notification.lead_name, contact, *extra) if p)
            parts.append(f"Lead: {who}.")
        if notification.lead_intent:
            intent = notification.lead_intent[:_MAX_INTENT].rstrip()
            parts.append(f"Wants: {intent}.")
        details = owner_facing_reasons(notification.reasons)
        if details:
            # Condensed like the intent: the row's phrases, one segment's worth at most.
            why = "; ".join(details)[:_MAX_INTENT].rstrip()
            parts.append(f"Why: {why}.")
        parts.append(notification.next_step)
        # The dashboard row's #ref — the SMS previously carried NO conversation handle.
        parts.append(f"#{notification.short_ref}")
        return " ".join(parts)

    async def send(
        self, session: AsyncSession, notification: EscalationNotification, *, to: str
    ) -> DeliveryReceipt:
        return await self.send_text(
            session,
            admin_id=notification.admin_id,
            instance_id=notification.instance_id,
            to=to,
            body=self.format(notification),
            status_callback=await notification_status_callback_url(
                session, notification.admin_id, notification.instance_id
            ),
        )

    async def send_text(
        self,
        session: AsyncSession,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        to: str,
        body: str,
        status_callback: str | None = None,
    ) -> DeliveryReceipt:
        """One text to the owner through THEIR Twilio, reported as data (never a raise).

        The escalation page, the contact-confirmation code (round 7 WP-4b) and the
        billing notices (round 7 WP-1 #3c: the card-removed notice reaches the
        escalation SMS number when one is on file) share this leg so the BYO rule and
        the honest-degradation rule are written once: no provisioned number →
        ``sms_channel_not_provisioned``; a live deployment whose sender resolved to the
        Null stand-in → ``sms_sender_not_provisioned`` (nothing left the system);
        outside live mode the Null sender's fake sid is reported with
        ``detail="recorded"`` so a caller can audit it as recorded, not sent. Through a
        real provider sender the receipt says ``dlr_pending``: accepted by the API,
        delivery still to be reported by the carrier. ``status_callback`` is where
        Twilio is asked to report it, on either credential shape.
        """
        from_number = await resolve_sms_number(session, admin_id, instance_id)
        if not from_number:
            return DeliveryReceipt(accepted=False, detail="sms_channel_not_provisioned")
        # BYO (§3.1.4): the page goes out through the tenant's OWN Twilio account, the
        # same one their customer SMS rides — the platform is not in the billing path.
        sender = await build_tenant_sms_sender(session, admin_id, instance_id)
        if status_callback and isinstance(sender, TwilioSmsSender | TwilioBearerSmsSender):
            # A copy: the tenant's own sender object is never mutated.
            sender = replace(sender, status_callback=status_callback)
        try:
            sid = await sender.send_sms(from_number, to, body)
        except Exception as exc:  # noqa: BLE001 — a send failure is data, not a raise
            return DeliveryReceipt(accepted=False, detail=f"twilio_error:{type(exc).__name__}")
        if isinstance(sender, NullSmsSender):
            if get_settings().integration_mode == "live":
                # Live deployment without complete Twilio credentials (see twilio_sender's
                # Account-SID gap): nothing left the system.
                return DeliveryReceipt(accepted=False, detail="sms_sender_not_provisioned")
            return DeliveryReceipt(accepted=True, provider_message_id=sid, detail="recorded")
        if sid is None:
            return DeliveryReceipt(accepted=False, detail="twilio_rejected")
        # Accepted by the provider's API — which is not the contact hearing, with or
        # without a callback requested.
        return DeliveryReceipt(accepted=True, provider_message_id=sid, detail=DLR_PENDING)
