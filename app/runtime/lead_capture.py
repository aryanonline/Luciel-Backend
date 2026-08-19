"""Lead capture + identity promotion cognition (Architecture §3.4.4 / §3.4.9).

Always-on cognition: when a conversation crosses the "this is a real lead"
threshold — the customer provides contact info, states intent about a specific
service, mentions a budget, or otherwise signals sales-qualified intent — the
runtime writes/updates a structured Lead row in VantageMind's own dashboard store
(§3.4.4). This is NOT the `push_to_crm` tool (which writes OUTWARD to an external
CRM); this is the platform-owned lead record cognition never lets the admin
disable.

Identity promotion (§3.4.9): a weak widget token carries no lead until the visitor
volunteers a strong identifier (email/phone) in-conversation, at which point a Lead
is created/linked and the session is bound to it (`resolved_lead_id`). Strong
channels (email/sms/voice/whatsapp) resolve a lead from the transport identifier
directly. A matching identifier NEVER authorizes PII disclosure — it only gives
continuity (the HARD RULE lives in sessions.py; this module only writes records).

Deterministic + dependency-free so the loop and tests run without an LLM; the
production extractor can bind behind `extract_lead_signals` unchanged.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Session
from app.models.lead import Lead, LeadState

# --- signal extraction (deterministic; production extractor binds here) ------ #

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# North-American-ish phone; kept permissive but anchored on 10+ digits.
_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
_INTENT_RE = re.compile(
    r"\b(book|buy|purchase|sign up|get started|schedule|hire|engage|interested in|"
    r"looking for|need help|quote|pricing|cost|how much)\b",
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(r"(\$\s?\d[\d,]*)|(\b\d+\s?k\b)|\bbudget\b", re.IGNORECASE)


@dataclass(frozen=True)
class LeadSignals:
    """What a message revealed about the visitor as a sales-qualified lead."""

    email: str | None
    phone: str | None
    has_intent: bool
    has_budget: bool

    @property
    def strong_identifier(self) -> str | None:
        """A strong, in-conversation-volunteered identifier that promotes a weak
        widget token to a real lead (§3.4.9)."""
        return self.email or self.phone

    @property
    def is_qualified(self) -> bool:
        """Crossed the 'this is a real lead' threshold (§3.4.4): contact info, OR
        (intent AND/OR budget) signalling a sales-qualified contact."""
        return bool(self.strong_identifier) or self.has_intent or self.has_budget


def extract_lead_signals(text: str) -> LeadSignals:
    t = text or ""
    email_m = _EMAIL_RE.search(t)
    phone_m = _PHONE_RE.search(t)
    return LeadSignals(
        email=email_m.group(0) if email_m else None,
        phone=phone_m.group(0) if phone_m else None,
        has_intent=bool(_INTENT_RE.search(t)),
        has_budget=bool(_BUDGET_RE.search(t)),
    )


def _summarize_intent(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:2040] + "…") if len(t) > 2040 else t


# --- capture / upsert -------------------------------------------------------- #


async def capture_or_update_lead(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    sess: Session,
    inbound_text: str,
    channel: str,
    transport_identifier: str | None,
) -> uuid.UUID | None:
    """Always-on lead-capture cognition (§3.4.4). Returns the resolved_lead_id if a
    lead exists/was created for this turn, else None.

    - Strong channels (email/sms/voice/whatsapp/messenger/instagram): the transport
      identifier IS the lead key — capture/upsert immediately on a qualified turn.
    - Weak widget: only promote to a real lead once the visitor volunteers a strong
      identifier (email/phone) in-conversation (§3.4.9). Before that, no lead row is
      written (the visitor is genuinely anonymous), matching the HARD RULE that a
      weak token never carries identity.

    Idempotent per (admin_id, instance_id, contact_identifier): a returning/continuing
    lead's record is UPDATED in place, never duplicated — mirroring the maintain-the-
    record intent (§3.4.4 / §3.3.6 for the CRM analogue).
    """
    from app.runtime.escalation import high_value_lead_score
    from app.runtime.sessions import IDENTITY_STRENGTH, IdentityStrength

    signals = extract_lead_signals(inbound_text)
    score = high_value_lead_score(inbound_text)
    strength = IDENTITY_STRENGTH.get(channel, IdentityStrength.weak)

    # Determine the stable contact key for this lead.
    if strength in (IdentityStrength.strong, IdentityStrength.platform_scoped_strong):
        # The transport identifier (phone/email/meta-id) is the lead key.
        contact_key = transport_identifier or signals.strong_identifier
    else:
        # Weak widget: a lead exists only once a strong identifier is volunteered.
        contact_key = signals.strong_identifier

    # If the session is already bound to a lead, keep maintaining that same record.
    if sess.resolved_lead_id is not None:
        lead = (
            await session.execute(
                select(Lead).where(Lead.admin_id == admin_id, Lead.id == sess.resolved_lead_id)
            )
        ).scalar_one_or_none()
        if lead is not None:
            _apply_signals(lead, signals, inbound_text, score)
            await session.flush()
            return lead.id

    # Identity IS capture on strong channels (live-caught 2026-08-18): a voice
    # caller's E.164 was DISCARDED because no intent keyword fired, and the
    # escalation email then said "Contact: not captured" about a caller whose
    # number we knew the whole time. When the transport itself names the person,
    # the lead exists — signals only enrich it. The widget rule is unchanged:
    # a weak token never carries identity, so an anonymous visitor still needs
    # to volunteer a strong identifier before any row is written.
    identity_is_capture = (
        strength in (IdentityStrength.strong, IdentityStrength.platform_scoped_strong)
        and bool(transport_identifier)
    )
    # No lead yet and nothing qualifies → stay anonymous (no row written).
    if contact_key is None or not (signals.is_qualified or identity_is_capture):
        return None

    # Upsert on the stable contact key (update-in-place, never duplicate).
    existing = (
        await session.execute(
            select(Lead).where(
                Lead.admin_id == admin_id,
                Lead.instance_id == instance_id,
                Lead.contact_identifier == contact_key,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        _apply_signals(existing, signals, inbound_text, score)
        lead_id = existing.id
    else:
        lead = Lead(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            name=None,
            contact_identifier=contact_key,
            # Intent only when the turn actually SIGNALED one: an identity-only
            # capture ("No its alright thankyou" from a known number) must not
            # store a pleasantry as the lead's recorded want.
            intent=_summarize_intent(inbound_text) if signals.is_qualified else None,
            lead_score=score if score > 0 else None,
            state=LeadState.active,
        )
        session.add(lead)
        lead_id = lead.id

    # Bind the session to the lead so escalation payloads + cross-session memory
    # (strong identities only) resolve against it going forward (§3.4.9).
    sess.resolved_lead_id = lead_id
    await session.flush()
    return lead_id


def _apply_signals(lead: Lead, signals: LeadSignals, inbound_text: str, score: float) -> None:
    """Maintain the lead's record: fill missing contact + keep the latest intent and
    the highest observed high-value score (§3.4.4 maintain-the-record)."""
    if lead.contact_identifier is None and signals.strong_identifier:
        lead.contact_identifier = signals.strong_identifier
    # Keep the most recent qualified intent as the working intent line.
    if signals.has_intent or signals.has_budget:
        lead.intent = _summarize_intent(inbound_text)
    # Keep the highest score seen across the lead's lifetime.
    if score > 0 and (lead.lead_score is None or score > lead.lead_score):
        lead.lead_score = score
