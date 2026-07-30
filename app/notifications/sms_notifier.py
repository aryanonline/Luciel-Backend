"""SMS escalation notifier (Architecture §3.5.1) — Twilio.

Same vendor as customer SMS, and it sends FROM the tenant's own provisioned number
(§3.1.4 BYO) — an owner being paged about their own business should see their own number,
and the platform has no other number to send from.

Requires the SMS channel to be provisioned. If it is not, that is reported as an
actionable failure rather than a silent drop, so the routing layer can fall back to the
owner's email (§3.5.4) instead of leaving the escalation undelivered.
"""

from __future__ import annotations

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
        """Condensed §3.5.1 payload: why, who to reach, what to do."""
        parts = [f"{notification.business_name}: Luciel escalated ({notification.signal})."]
        if notification.lead_name or notification.lead_contact:
            who = " ".join(p for p in (notification.lead_name, notification.lead_contact) if p)
            parts.append(f"Lead: {who}.")
        if notification.lead_intent:
            intent = notification.lead_intent[:_MAX_INTENT].rstrip()
            parts.append(f"Wants: {intent}.")
        parts.append(notification.next_step)
        return " ".join(parts)

    async def send(
        self, session: AsyncSession, notification: EscalationNotification, *, to: str
    ) -> DeliveryReceipt:
        from_number = await resolve_sms_number(
            session, notification.admin_id, notification.instance_id
        )
        if not from_number:
            return DeliveryReceipt(accepted=False, detail="sms_channel_not_provisioned")
        # BYO (§3.1.4): the page goes out through the tenant's OWN Twilio account, the
        # same one their customer SMS rides — the platform is not in the billing path.
        sender = await build_tenant_sms_sender(
            session, notification.admin_id, notification.instance_id
        )
        try:
            sid = await sender.send_sms(from_number, to, self.format(notification))
        except Exception as exc:  # noqa: BLE001 — a send failure is data, not a raise
            return DeliveryReceipt(accepted=False, detail=f"twilio_error:{type(exc).__name__}")
        if isinstance(sender, NullSmsSender) and get_settings().integration_mode == "live":
            # Live deployment without complete Twilio credentials (see twilio_sender's
            # Account-SID gap): nothing left the system.
            return DeliveryReceipt(accepted=False, detail="sms_sender_not_provisioned")
        if sid is None:
            return DeliveryReceipt(accepted=False, detail="twilio_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=sid)
