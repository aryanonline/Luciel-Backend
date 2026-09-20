"""Admin-notification channel adapter contract (Architecture §3.5.1).

Escalation delivery has exactly two channels — email and SMS (§3.5.3) — and each one
implements the same small contract so the routing layer (``escalation_dispatch``) never
knows which transport it is talking to. Delivery receipts reuse the channel layer's
``DeliveryReceipt`` rather than introducing a second near-identical type.

The notification body is the product, not an afterthought: §3.5.1 requires every
escalation notification to carry the lead summary AND the lead's contact info, so the
recipient can act **without logging in to the dashboard**. ``format`` is on the contract
because email and SMS render that same payload very differently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryReceipt

# What the recipient should actually DO, per signal (§3.5.1 "recommended next step").
_NEXT_STEP: dict[str, str] = {
    "explicit_human_request": "The lead asked to speak to a person — reply to them directly.",
    "strong_negative_sentiment": (
        "The lead is unhappy. A personal reply now is the difference between a save and a "
        "lost customer."
    ),
    "cannot_answer": (
        "Luciel could not answer this confidently — send the answer yourself, then consider "
        "adding it to your knowledge base."
    ),
    "high_value_lead": "High-value lead — follow up while they are still engaged.",
    "budget_exhausted": (
        "This Luciel hit its included conversation limit and stopped answering. Add a payment "
        "method to keep it running, then follow up with this lead."
    ),
    "llm_unavailable": (
        "Luciel could not reach its model provider and could not answer. Reply manually — the "
        "platform is retrying automatically."
    ),
    "luciel_requested_handoff": (
        "Luciel judged that a person should step in — read its reason, then reply to the "
        "lead directly. Luciel keeps answering in the meantime."
    ),
    "reply_undeliverable": (
        "Luciel wrote a reply the channel could not deliver — the customer is waiting. "
        "Reach them another way and check the channel's connection."
    ),
}


@dataclass(frozen=True)
class EscalationNotification:
    """The §3.5.1 escalation payload — everything needed to act without logging in.

    The 2026-08-18 enrichment (live-caught: the email named nothing but a bare UUID
    while the caller's phone number was sitting in the session): channel, when, the
    customer's transport identity where the channel provides one, the message that
    triggered the escalation, and a dashboard deep link. All optional/derived at
    send time so the deferred-queue replay path needs no payload change and no PII
    lands in the queue."""

    admin_id: uuid.UUID
    instance_id: uuid.UUID
    session_id: uuid.UUID
    signal: str
    gate: str
    business_name: str
    lead_name: str | None = None
    lead_contact: str | None = None
    lead_intent: str | None = None
    lead_score: float | None = None
    channel: str | None = None
    fired_at: datetime | None = None
    customer_identifier: str | None = None
    triggering_message: str | None = None
    dashboard_url: str | None = None
    # Why it fired, in short phrases (round 6 WP-G, F124) — read from the escalation
    # row at send time (round 7 WP-2, item 2): for ``luciel_requested_handoff`` this is
    # the model's own reason, which is the whole point of that page.
    reasons: tuple[str, ...] = ()

    @property
    def conversation_ref(self) -> str:
        """Non-identifying session handle — the only lead reference safe for an audit
        payload (§5.2 / §3.4.11: never the lead's raw contact)."""
        return str(self.session_id)

    @property
    def short_ref(self) -> str:
        """First 8 of the session id — matches the dashboard row's #ref."""
        return str(self.session_id)[:8]

    @property
    def next_step(self) -> str:
        return _NEXT_STEP.get(self.signal, "Review this conversation and follow up.")


class NotificationAdapter(Protocol):
    """One admin-notification channel (§3.5.1). email | sms only."""

    channel_id: str

    async def send(
        self, session: AsyncSession, notification: EscalationNotification, *, to: str
    ) -> DeliveryReceipt:
        """Send the notification; report the outcome as data (never raise on a delivery
        problem — the escalation row is already the authoritative record, §3.5.2)."""
        ...

    def verify_config(self, to: str) -> bool:
        """Is this a well-formed contact for this channel? A malformed contact is
        reported as an actionable failure instead of being handed to the provider."""
        ...

    def format(self, notification: EscalationNotification) -> str:
        """Render the §3.5.1 payload per this channel's conventions."""
        ...
