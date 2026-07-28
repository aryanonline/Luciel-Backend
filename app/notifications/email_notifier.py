"""Email escalation notifier (Architecture §3.5.1) — AWS SES.

Same SES transport as every other platform notice (``email_sender.build_email_sender``),
so no new vendor and no new DPA. This adapter owns the escalation email *rendering*; the
routing layer (``escalation_dispatch``) owns the audit trail, so this module emits no
audit events of its own — one writer for the §3.5.5 event stream.

Honest degradation: in live mode with no platform sender identity bound, SES is not
actually reachable and ``build_email_sender`` hands back the Null sender. Reporting that
as delivered would be a silent drop of the one notification the owner is waiting for, so
it is reported as an actionable failure instead (§3.5.4).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryReceipt
from app.config import get_settings
from app.notifications.base import EscalationNotification
from app.notifications.email_sender import NullEmailSender, build_email_sender

_SIGNAL_LABELS: dict[str, str] = {
    "explicit_human_request": "the lead asked for a person",
    "strong_negative_sentiment": "the lead is unhappy",
    "cannot_answer": "Luciel could not answer confidently",
    "high_value_lead": "high-value lead",
    "budget_exhausted": "conversation limit reached",
    "llm_unavailable": "Luciel could not reach its model provider",
}


class EmailNotifier:
    channel_id = "email"

    def verify_config(self, to: str) -> bool:
        # Deliberately shallow: SES is the real validator. This only catches a contact
        # that could never be an address, so it fails fast with an actionable reason.
        return "@" in to and " " not in to.strip() and not to.startswith("@")

    def subject(self, notification: EscalationNotification) -> str:
        label = _SIGNAL_LABELS.get(notification.signal, notification.signal)
        return f"Luciel needs you: {label}"

    def format(self, notification: EscalationNotification) -> str:
        """The §3.5.1 payload as an email body: summary, contact info, next step."""
        lines = [
            f"{notification.business_name}'s Luciel escalated a conversation.",
            "",
            f"Why: {_SIGNAL_LABELS.get(notification.signal, notification.signal)}",
        ]
        if notification.lead_intent:
            lines.append(f"What they want: {notification.lead_intent}")
        if notification.lead_score is not None:
            lines.append(f"Lead score: {notification.lead_score:.2f}")
        lines += ["", "Who to reach:"]
        lines.append(f"  Name: {notification.lead_name or 'not captured'}")
        lines.append(f"  Contact: {notification.lead_contact or 'not captured'}")
        lines += [
            "",
            f"What to do: {notification.next_step}",
            "",
            f"Conversation: {notification.conversation_ref}",
            "Open your dashboard to read the full transcript or take over the conversation.",
        ]
        return "\n".join(lines)

    async def send(
        self, session: AsyncSession, notification: EscalationNotification, *, to: str
    ) -> DeliveryReceipt:
        sender = build_email_sender()
        try:
            message_id = await sender.send(
                to=to, subject=self.subject(notification), body_text=self.format(notification)
            )
        except Exception as exc:  # noqa: BLE001 — a send failure is data, not a raise
            return DeliveryReceipt(accepted=False, detail=f"ses_error:{type(exc).__name__}")
        if isinstance(sender, NullEmailSender) and get_settings().integration_mode == "live":
            # Live deployment with no verified sender identity: nothing left the system.
            return DeliveryReceipt(accepted=False, detail="email_sender_not_provisioned")
        return DeliveryReceipt(accepted=True, provider_message_id=message_id)
