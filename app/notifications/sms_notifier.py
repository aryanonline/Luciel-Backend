"""SMS escalation notifier (Architecture §3.5.1) — Twilio.

Same vendor as customer SMS, and it sends FROM the tenant's own provisioned number
(§3.1.4 BYO) — an owner being paged about their own business should see their own number,
and the platform has no other number to send from.

Requires the SMS channel to be provisioned. If it is not, that is reported as an
actionable failure rather than a silent drop, so the routing layer can fall back to the
owner's email (§3.5.4) instead of leaving the escalation undelivered.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryReceipt
from app.channels.tenant_twilio import build_tenant_sms_sender
from app.channels.twilio_sender import NullSmsSender
from app.config import get_settings
from app.connections.service import resolve_sms_number
from app.notifications.base import EscalationNotification

# One SMS segment is 160 GSM-7 chars; the body is condensed to stay in a small number of
# segments (§3.5.1 "SMS condensed form") while keeping the contact info, which is the
# whole point of paging someone.
_MAX_INTENT = 80


class SmsNotifier:
    channel_id = "sms"

    def verify_config(self, to: str) -> bool:
        # E.164: leading + and 2-15 digits. Twilio is the real validator; this only
        # catches a contact that could never be a number.
        digits = to[1:] if to.startswith("+") else ""
        return to.startswith("+") and digits.isdigit() and 2 <= len(digits) <= 15

    def format(self, notification: EscalationNotification) -> str:
        """Condensed §3.5.1 payload: why, who to reach, what to do, which conversation."""
        parts = [f"{notification.business_name}: Luciel escalated ({notification.signal})."]
        contact = notification.lead_contact or notification.customer_identifier
        if notification.lead_name or contact:
            who = " ".join(p for p in (notification.lead_name, contact) if p)
            parts.append(f"Lead: {who}.")
        if notification.lead_intent:
            intent = notification.lead_intent[:_MAX_INTENT].rstrip()
            parts.append(f"Wants: {intent}.")
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
        )

    async def send_text(
        self,
        session: AsyncSession,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        to: str,
        body: str,
    ) -> DeliveryReceipt:
        """One text to the owner through THEIR Twilio, reported as data (never a raise).

        The escalation page and the billing notices (round 7 WP-1 #3c: the card-removed
        notice reaches the escalation SMS number when one is on file) share this leg so
        the BYO rule and the honest-degradation rule are written once: no provisioned
        number → ``sms_channel_not_provisioned``; a live deployment whose sender
        resolved to the Null stand-in → ``sms_sender_not_provisioned`` (nothing left the
        system); outside live mode the Null sender's fake sid is reported with
        ``detail="recorded"`` so a caller can audit it as recorded, not sent.
        """
        from_number = await resolve_sms_number(session, admin_id, instance_id)
        if not from_number:
            return DeliveryReceipt(accepted=False, detail="sms_channel_not_provisioned")
        # BYO (§3.1.4): the page goes out through the tenant's OWN Twilio account, the
        # same one their customer SMS rides — the platform is not in the billing path.
        sender = await build_tenant_sms_sender(session, admin_id, instance_id)
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
        return DeliveryReceipt(accepted=True, provider_message_id=sid)
