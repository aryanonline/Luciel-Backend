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
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import Session
from app.models.lead import Lead, LeadIdentifier, LeadState

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw: str | None) -> str | None:
    """A volunteered or transport phone → E.164, or None when it cannot honestly be one
    (2026-09-05 audit F111/F127). Ten North-American digits gain +1 (the offering
    region); an explicit ``+`` keeps its own country code; anything else is left
    unresolved rather than guessed — a wrong number in the CRM is worse than none."""
    if not raw:
        return None
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if text.startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits[0] == "1":
        candidate = "+" + digits
    else:
        return None
    return candidate if _E164_RE.match(candidate) else None


def identifier_kind(value: str) -> str:
    """``email`` / ``phone`` (E.164) / ``transport`` (a platform-scoped id)."""
    if "@" in value:
        return "email"
    if _E164_RE.match(value.strip()):
        return "phone"
    return "transport"


def normalize_identifier(value: str) -> str:
    """Emails case-fold; phones and transport ids are compared verbatim."""
    text = value.strip()
    return text.lower() if "@" in text else text


def is_email_address(value: str) -> bool:
    """Whole-string email shape (the owner-edit validator, round 7 WP-3c item 6)."""
    return _EMAIL_RE.fullmatch(value.strip()) is not None


def crm_phone_key(lead: Lead) -> str | None:
    """The E.164 key a custom webhook may dedupe on when no email exists (F126)."""
    for candidate in (lead.phone, lead.contact_identifier):
        if candidate and _E164_RE.match(candidate.strip()):
            return candidate.strip()
    return None


def crm_lead_key(lead: Lead) -> str | None:
    """The email key the CRM push dedupes on (§3.3.6, round 5A item 5).

    Prefers the volunteered ``email`` column (a phone-keyed SMS/voice lead who
    later shared an address), falls back to an email-shaped
    ``contact_identifier``. None means the lead has no CRM-usable key yet — the
    push honestly declines rather than keying a CRM record on a phone number."""
    if lead.email and "@" in lead.email:
        return lead.email
    if lead.contact_identifier and "@" in lead.contact_identifier:
        return lead.contact_identifier
    return None


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
# #5d (live-caught 2026-08-23): "I'm Jane Miller" left the lead named "Unknown" —
# in the dashboard AND in the Salesforce push, whose required LastName then read
# the honest-but-avoidable "Unknown" fallback. Conservative by design: only an
# explicit self-introduction lead-in, followed by 1–3 CAPITALIZED tokens (the
# case-sensitive name group is what rejects "I'm interested in pricing" — the
# lead-in match is case-insensitive, the name is not). A miss costs nothing (the
# name stays unfilled and the CRM fallback stands); a wrong capture pollutes the
# tenant's CRM, so precision wins over recall.
# A word starts with a capital and is at least two characters: a lowercase run
# ("Jane") or an apostrophe/hyphen joint straight after the capital ("O'Brien"),
# with further joints allowed ("Jean-Luc"). A bare single capital never matches.
_NAME_WORD = r"[A-Z](?:[a-z]+|['’-][A-Z][a-z]+)(?:['’-][A-Z][a-z]+)*"
_NAME_RE = re.compile(
    rf"\b(?i:my name is|i am|i'm|i’m|this is)[ \t]+({_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,2}})"
)
# Capitalized sentence-continuations that follow the same lead-ins in real chat
# ("I'm Interested…", "Hi, this is Urgent") — never treat these as a first name.
_NAME_STOPWORDS = frozenset(
    {
        "Actually",
        "Afraid",
        "Available",
        "Busy",
        "Calling",
        "Curious",
        "Fine",
        "Free",
        "Good",
        "Happy",
        "Here",
        "Hoping",
        "Interested",
        "Just",
        "Looking",
        "New",
        "Not",
        "Okay",
        "Only",
        "Open",
        "Ready",
        "Really",
        "Sorry",
        "Still",
        "Sure",
        "The",
        "Trying",
        "Urgent",
        "Wondering",
        "Writing",
        # Scheduling turns capitalize these after the same lead-ins ("I'm Free
        # Friday", "I am Monday…") — a closed set, never a first name here.
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
)


def _extract_name(text: str) -> str | None:
    m = _NAME_RE.search(text or "")
    if m is None:
        return None
    name = m.group(1)
    if name.split()[0] in _NAME_STOPWORDS:
        return None
    return name


@dataclass(frozen=True)
class LeadSignals:
    """What a message revealed about the visitor as a sales-qualified lead."""

    email: str | None
    phone: str | None
    has_intent: bool
    has_budget: bool
    # Self-introduced name ("I'm Jane Miller") — fill-when-absent only; a
    # conservative miss is None (#5d).
    name: str | None = None

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
        # E.164 from the first turn (F111/F127): the widget visitor who types
        # 416-555-0143 and the SMS sender +14165550143 are one person, and only a
        # normalised key lets the lookup see that.
        phone=normalize_phone(phone_m.group(0)) if phone_m else None,
        has_intent=bool(_INTENT_RE.search(t)),
        has_budget=bool(_BUDGET_RE.search(t)),
        name=_extract_name(t),
    )


def _summarize_intent(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:2040] + "…") if len(t) > 2040 else t


# --- identifiers (F125/F127) -------------------------------------------------- #


def _lead_identifier_values(lead: Lead) -> set[str]:
    return {normalize_identifier(v) for v in (lead.contact_identifier, lead.email, lead.phone) if v}


async def record_identifiers(
    session: AsyncSession, lead: Lead, *, confirmed_transport: str | None = None
) -> None:
    """Make sure every identifier the row carries has its ``lead_identifiers`` row.

    Idempotent (unique on tenant+kind+value, conflicts ignored). A value already
    owned by ANOTHER lead is left alone — that is the unification signal, not ours."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    for value in _lead_identifier_values(lead):
        await session.execute(
            pg_insert(LeadIdentifier)
            .values(
                id=uuid.uuid4(),
                admin_id=lead.admin_id,
                instance_id=lead.instance_id,
                lead_id=lead.id,
                kind=identifier_kind(value),
                value=value,
            )
            .on_conflict_do_nothing(constraint="uq_lead_identifier")
        )
    if confirmed_transport:
        # Round 6 WP-H (F060): the channel itself named the person — that identifier
        # is confirmed at capture, so a later "email me" on SMS can go to their
        # number without asking, and never to an address they did not confirm.
        from app.runtime.channel_switch import confirm_identifier  # noqa: PLC0415

        key = normalize_identifier(confirmed_transport)
        await confirm_identifier(
            session,
            admin_id=lead.admin_id,
            instance_id=lead.instance_id,
            lead_id=lead.id,
            kind=identifier_kind(key),
            value=key,
            source="transport",
        )


#: Identifier-row kinds that RESOLVE a lead. ``mention`` (round 7 WP-3c item 2: an
#: address the customer mentioned without claiming it) and ``superseded`` (item 6: a
#: value an owner edit replaced) are kept for the record and never resolve a
#: conversation, never key a CRM push, and never receive a delivery.
RESOLVING_KINDS: frozenset[str] = frozenset({"email", "phone", "transport"})
MENTION_KIND = "mention"
SUPERSEDED_KIND = "superseded"


async def supersede_identifier(
    session: AsyncSession,
    lead: Lead,
    *,
    kind: str,
    old: str | None,
    new: str | None,
) -> None:
    """An owner edit replaced ``old`` with ``new`` on the lead (round 7 WP-3c item 6).

    The replaced value is re-kinded to ``superseded`` rather than dropped — the
    record still says the person was once known by it — and it stops resolving
    anything from this moment. The new value gets its own resolving row, recorded
    as owner-entered and NOT confirmed: only the customer can confirm an address
    (F060), so a channel switch will still ask before sending there."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    if old:
        old_key = normalize_identifier(old)
        already = (
            await session.execute(
                select(LeadIdentifier.id).where(
                    LeadIdentifier.admin_id == lead.admin_id,
                    LeadIdentifier.instance_id == lead.instance_id,
                    LeadIdentifier.kind == SUPERSEDED_KIND,
                    LeadIdentifier.value == old_key,
                )
            )
        ).scalar_one_or_none()
        if already is None:
            await session.execute(
                update(LeadIdentifier)
                .where(
                    LeadIdentifier.admin_id == lead.admin_id,
                    LeadIdentifier.instance_id == lead.instance_id,
                    LeadIdentifier.lead_id == lead.id,
                    LeadIdentifier.kind == kind,
                    LeadIdentifier.value == old_key,
                )
                .values(kind=SUPERSEDED_KIND, confirmed_at=None, source="owner")
            )
        else:
            await session.execute(
                delete(LeadIdentifier).where(
                    LeadIdentifier.admin_id == lead.admin_id,
                    LeadIdentifier.lead_id == lead.id,
                    LeadIdentifier.kind == kind,
                    LeadIdentifier.value == old_key,
                )
            )
    if new:
        await session.execute(
            pg_insert(LeadIdentifier)
            .values(
                id=uuid.uuid4(),
                admin_id=lead.admin_id,
                instance_id=lead.instance_id,
                lead_id=lead.id,
                kind=kind,
                value=normalize_identifier(new),
                source="owner",
            )
            .on_conflict_do_nothing(constraint="uq_lead_identifier")
        )


async def find_lead_by_identifier(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    value: str,
    exclude_id: uuid.UUID | None = None,
) -> Lead | None:
    """The oldest lead known by ``value`` — on its own columns OR in the identifier
    table (an identity absorbed by a merge). Emails case-fold; phones must already
    be E.164 (callers normalise). Deterministic: oldest row wins."""
    key = normalize_identifier(value)
    kind = identifier_kind(key)
    if kind == "email":
        own = [func.lower(Lead.contact_identifier) == key, Lead.email == key]
    else:
        own = [Lead.contact_identifier == key, Lead.phone == key]
    known = select(LeadIdentifier.lead_id).where(
        LeadIdentifier.admin_id == admin_id,
        LeadIdentifier.instance_id == instance_id,
        LeadIdentifier.kind == kind,
        LeadIdentifier.value == key,
    )
    stmt = select(Lead).where(
        Lead.admin_id == admin_id,
        Lead.instance_id == instance_id,
        or_(*own, Lead.id.in_(known)),
    )
    if exclude_id is not None:
        stmt = stmt.where(Lead.id != exclude_id)
    return (
        (await session.execute(stmt.order_by(Lead.created_at.asc(), Lead.id.asc()).limit(1)))
        .scalars()
        .first()
    )


# --- capture / upsert -------------------------------------------------------- #


@dataclass(frozen=True)
class CaptureResult:
    """One turn's capture outcome: which lead, and which facts actually changed.

    ``changed`` is what the deterministic CRM push (Phase 6.5) forwards — pushing
    only on real change is what keeps "keeps each lead's record current" from
    degrading into a same-payload call to the tenant's CRM on every turn.
    ``lead_key`` is the lead's stable contact identifier after this turn."""

    lead_id: uuid.UUID
    changed: dict[str, str]
    lead_key: str | None = None
    # Snapshot of the record's owner-facing facts (round 5C item 6): what the
    # runtime injects as lead context so Luciel stops re-asking a name it has on
    # file. NEVER contact identifiers — a matching identifier never authorizes
    # PII readback (§3.4.9).
    known_name: str | None = None
    known_intent: str | None = None


async def capture_or_update_lead(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    sess: Session,
    inbound_text: str,
    channel: str,
    transport_identifier: str | None,
) -> CaptureResult | None:
    """Always-on lead-capture cognition (§3.4.4). Returns the capture result (the
    resolved lead id + the facts THIS turn changed) if a lead exists/was created
    for this turn, else None.

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
    # An identifier the transport carried is the customer's own (F060).
    transport_confirmed = (
        transport_identifier
        if strength in (IdentityStrength.strong, IdentityStrength.platform_scoped_strong)
        else None
    )

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
            # Round 5A item 7: a volunteered identifier that belongs to a
            # DIFFERENT existing row proves two records are one person — unify
            # instead of maintaining a duplicate. Exact-equality only (emails
            # case-folded); the older row survives; the turn continues against
            # the survivor.
            volunteered = signals.strong_identifier
            if volunteered:
                if normalize_identifier(volunteered) not in _lead_identifier_values(lead):
                    # Identifier-aware (F125): an identity absorbed by an earlier
                    # merge still proves the match.
                    other = await find_lead_by_identifier(
                        session,
                        admin_id=admin_id,
                        instance_id=instance_id,
                        value=volunteered,
                        exclude_id=lead.id,
                    )
                    if other is not None:
                        keep, absorb = (
                            (other, lead) if other.created_at <= lead.created_at else (lead, other)
                        )
                        gained = await _unify_leads(
                            session, admin_id=admin_id, keep=keep, absorb=absorb
                        )
                        sess.resolved_lead_id = keep.id
                        lead = keep
                        changed = _apply_signals(lead, signals, inbound_text, score)
                        changed.update(gained)
                        lead.last_activity_at = datetime.now(UTC)
                        await _reactivate_if_archived(session, lead)
                        await session.flush()
                        await record_identifiers(
                            session, lead, confirmed_transport=transport_confirmed
                        )
                        return CaptureResult(
                            lead_id=lead.id,
                            changed=changed,
                            lead_key=crm_lead_key(lead),
                            known_name=lead.name,
                            known_intent=lead.intent,
                        )
            changed = _apply_signals(lead, signals, inbound_text, score)
            # Item 4: activity is a WRITE, not an inference — auto-prune measures
            # inactivity against this, and it was never written before (a lead
            # conversing for months read as stale as their creation date).
            lead.last_activity_at = datetime.now(UTC)
            await _reactivate_if_archived(session, lead)
            await session.flush()
            await record_identifiers(session, lead, confirmed_transport=transport_confirmed)
            return CaptureResult(
                lead_id=lead.id,
                changed=changed,
                lead_key=crm_lead_key(lead),
                known_name=lead.name,
                known_intent=lead.intent,
            )

    # Identity IS capture on strong channels (live-caught 2026-08-18): a voice
    # caller's E.164 was DISCARDED because no intent keyword fired, and the
    # escalation email then said "Contact: not captured" about a caller whose
    # number we knew the whole time. When the transport itself names the person,
    # the lead exists — signals only enrich it. The widget rule is unchanged:
    # a weak token never carries identity, so an anonymous visitor still needs
    # to volunteer a strong identifier before any row is written.
    identity_is_capture = strength in (
        IdentityStrength.strong,
        IdentityStrength.platform_scoped_strong,
    ) and bool(transport_identifier)
    # No lead yet and nothing qualifies → stay anonymous (no row written).
    if contact_key is None or not (signals.is_qualified or identity_is_capture):
        return None

    # Upsert on the stable contact key (update-in-place, never duplicate).
    # Round 5A item 7: an email key also matches a lead that knows that email
    # under EITHER column, case-folded — the returning visitor typing the same
    # address resolves to their existing record instead of duplicating it.
    # order_by replaces scalar_one_or_none's implicit uniqueness assumption:
    # the oldest row wins, deterministically (pre-existing duplicates absorb
    # lazily via the bound-branch unification).
    existing = await find_lead_by_identifier(
        session, admin_id=admin_id, instance_id=instance_id, value=contact_key
    )

    if existing is not None:
        changed = _apply_signals(existing, signals, inbound_text, score)
        existing.last_activity_at = datetime.now(UTC)  # item 4: real inactivity
        await _reactivate_if_archived(session, existing)
        lead_id = existing.id
        lead_key = crm_lead_key(existing)
        known_name, known_intent = existing.name, existing.intent
    else:
        intent = _summarize_intent(inbound_text) if signals.is_qualified else None
        # Item 5: a phone/transport-keyed capture that ALSO volunteered an email
        # stores it (normalized lower-case, matching participant-key folding) —
        # the CRM key exists from the very first turn.
        volunteered_email = (
            signals.email.strip().lower()
            if signals.email and signals.email.strip().lower() != contact_key.strip().lower()
            else None
        )
        # F111: a phone volunteered on the capturing turn is stored E.164 beside a
        # non-phone key (an email-keyed visitor, a Meta id) instead of being lost.
        volunteered_phone = (
            signals.phone if signals.phone and signals.phone != contact_key.strip() else None
        )
        lead = Lead(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            # A self-introduction on the capturing turn names the record from the
            # start ("I'm Jane Miller, my email is…" used to create name=None and
            # push LastName "Unknown" — #5d).
            name=signals.name,
            contact_identifier=contact_key,
            email=volunteered_email,
            phone=volunteered_phone,
            # Intent only when the turn actually SIGNALED one: an identity-only
            # capture ("No its alright thankyou" from a known number) must not
            # store a pleasantry as the lead's recorded want.
            intent=intent,
            lead_score=score if score > 0 else None,
            state=LeadState.active,
        )
        session.add(lead)
        lead_id = lead.id
        lead_key = crm_lead_key(lead)
        known_name, known_intent = lead.name, lead.intent
        changed = {"contact": contact_key}
        if volunteered_email:
            changed["email"] = volunteered_email
        if signals.name:
            changed["name"] = signals.name
        if intent:
            changed["intent"] = intent
        if signals.phone:
            changed["phone"] = signals.phone

    # Bind the session to the lead so escalation payloads + cross-session memory
    # (strong identities only) resolve against it going forward (§3.4.9).
    sess.resolved_lead_id = lead_id
    await session.flush()
    captured_row = existing if existing is not None else lead
    if captured_row is not None:
        await record_identifiers(session, captured_row, confirmed_transport=transport_confirmed)
    return CaptureResult(
        lead_id=lead_id,
        changed=changed,
        lead_key=lead_key,
        known_name=known_name,
        known_intent=known_intent,
    )


_OUTCOME_RANK = {"in_progress": 0, "lost": 1, "converted": 2}


async def _unify_leads(
    session: AsyncSession, *, admin_id: uuid.UUID, keep: Lead, absorb: Lead
) -> dict[str, str]:
    """Merge two rows proven to be the same person (round 5A item 7).

    Trigger: exact identifier equality (emails case-folded) discovered at
    capture time — the same visitor returning under a second identity used to
    become a duplicate Lead AND a duplicate CRM record with no way back.
    Survivor = the older row. The loser is hard-deleted (there is no tombstone
    state for leads — mirrors "prune is not a state") after every
    resolved_lead_id carrier re-points. Scope boundary: any CRM-side duplicate
    the loser created is NOT merged there — §3.3.6 is one-way; future pushes
    maintain the survivor's record only. Returns the facts the survivor gained
    (they ride the turn's CRM push)."""
    from app.models.conversation import EscalationEvent, SessionSummary  # noqa: PLC0415
    from app.models.crm_link import CrmLeadLink  # noqa: PLC0415
    from app.models.deferred import DeferredAction  # noqa: PLC0415

    gained: dict[str, str] = {}
    if keep.name is None and absorb.name:
        keep.name = absorb.name
        gained["name"] = absorb.name
    if keep.email is None and absorb.email:
        keep.email = absorb.email
        gained["email"] = absorb.email
    if keep.contact_identifier is None and absorb.contact_identifier:
        keep.contact_identifier = absorb.contact_identifier
        gained["contact"] = absorb.contact_identifier
    if keep.phone is None and absorb.phone:
        keep.phone = absorb.phone
        gained["phone"] = absorb.phone
    if absorb.intent and (keep.intent is None or absorb.last_activity_at > keep.last_activity_at):
        if absorb.intent != keep.intent:
            keep.intent = absorb.intent
            gained["intent"] = absorb.intent
    if absorb.lead_score is not None and (
        keep.lead_score is None or absorb.lead_score > keep.lead_score
    ):
        keep.lead_score = absorb.lead_score
    # An opt-out on EITHER identity must survive (§3.3.5 callback safety).
    keep.opted_out = keep.opted_out or absorb.opted_out
    if LeadState.active in (keep.state, absorb.state):
        keep.state = LeadState.active
    if _OUTCOME_RANK.get(absorb.outcome, 0) > _OUTCOME_RANK.get(keep.outcome, 0):
        keep.outcome = absorb.outcome
    keep.last_activity_at = max(keep.last_activity_at, absorb.last_activity_at)

    # Re-point every resolved_lead_id carrier before the loser row goes.
    for model in (Session, SessionSummary, EscalationEvent, DeferredAction):
        await session.execute(
            update(model)
            .where(model.admin_id == admin_id, model.resolved_lead_id == absorb.id)
            .values(resolved_lead_id=keep.id)
        )
    # F125: the absorbed identity keeps resolving — its identifiers now name the
    # survivor (the survivor's own rows are (re)recorded by the caller).
    await session.execute(
        update(LeadIdentifier)
        .where(LeadIdentifier.admin_id == admin_id, LeadIdentifier.lead_id == absorb.id)
        .values(lead_id=keep.id)
    )
    # CRM links: re-point the loser's rows unless the survivor already holds a
    # link for that provider — then the loser's is the duplicate, delete it.
    absorb_links = (
        (
            await session.execute(
                select(CrmLeadLink).where(
                    CrmLeadLink.admin_id == admin_id, CrmLeadLink.lead_id == absorb.id
                )
            )
        )
        .scalars()
        .all()
    )
    for link in absorb_links:
        survivor_has = (
            await session.execute(
                select(CrmLeadLink.id).where(
                    CrmLeadLink.admin_id == admin_id,
                    CrmLeadLink.instance_id == link.instance_id,
                    CrmLeadLink.provider == link.provider,
                    CrmLeadLink.lead_id == keep.id,
                )
            )
        ).scalar_one_or_none()
        if survivor_has is not None:
            await session.delete(link)
        else:
            link.lead_id = keep.id

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=keep.instance_id,
        actor_type=ActorType.luciel_runtime,
        event_type="lead_merged",
        payload={
            "surviving_lead_id_hash": audit.hash_lead_id(keep.id),
            "merged_lead_id_hash": audit.hash_lead_id(absorb.id),
            "reason": "identifier_match",
        },
    )
    await session.delete(absorb)
    await session.flush()
    return gained


async def _reactivate_if_archived(session: AsyncSession, lead: Lead) -> None:
    """Archived → active on inbound contact (§3.4.10a, round 5C item 6).

    The Journey promises a returning archived lead is "recognized and history
    restored". The sessions-module reactivation hook only ran when a caller
    passed a resolved_lead_id at session-open — which no live channel path does
    (leads resolve INSIDE the turn, here) — so reactivation was dead code in
    production and an archived lead kept conversing while reading archived in
    the dashboard."""
    if lead.state != LeadState.archived:
        return
    lead.state = LeadState.active
    await audit.emit(
        session,
        admin_id=lead.admin_id,
        instance_id=lead.instance_id,
        actor_type=ActorType.platform,
        event_type="lead_reactivated",
        payload={"lead_id": str(lead.id)},
    )


def _apply_signals(
    lead: Lead, signals: LeadSignals, inbound_text: str, score: float
) -> dict[str, str]:
    """Maintain the lead's record: fill missing contact + keep the latest intent and
    the highest observed high-value score (§3.4.4 maintain-the-record). Returns the
    facts this turn actually changed (the CRM push forwards exactly these)."""
    changed: dict[str, str] = {}
    if lead.contact_identifier is None and signals.strong_identifier:
        lead.contact_identifier = signals.strong_identifier
        changed["contact"] = signals.strong_identifier
    # Fill-when-absent only (#5d): a later "I'm Jane" never overwrites a name the
    # record already carries — the extractor is conservative, not authoritative.
    if lead.name is None and signals.name:
        lead.name = signals.name
        changed["name"] = signals.name
    # Item 5: a phone-keyed lead volunteering an email finally gets it STORED —
    # it used to be discarded entirely (contact_identifier was already set and
    # nothing else had a home for it). Fill-when-absent: the first volunteered
    # address is the CRM dedupe key from then on; a later different address is a
    # correction a human should make, not a silent rekey.
    incoming_email = signals.email.strip().lower() if signals.email else None
    if (
        lead.email is None
        and incoming_email
        and incoming_email != (lead.contact_identifier or "").strip().lower()
    ):
        lead.email = incoming_email
        changed["email"] = incoming_email
        # This is the first moment a phone-keyed lead CAN reach the CRM (the
        # push refuses non-email keys), so the record's known facts ride along —
        # otherwise the created CRM record would carry nothing but the key.
        if lead.name:
            changed.setdefault("name", lead.name)
        if lead.intent:
            changed.setdefault("intent", lead.intent)
        if lead.contact_identifier and "@" not in lead.contact_identifier:
            changed.setdefault("phone", lead.contact_identifier)
    # Keep the most recent qualified intent as the working intent line.
    if signals.has_intent or signals.has_budget:
        intent = _summarize_intent(inbound_text)
        if intent != lead.intent:
            lead.intent = intent
            changed["intent"] = intent
    # F111: a phone volunteered mid-conversation is STORED (E.164, fill-when-absent)
    # and forwarded; a later different number is still a reachable fact for the CRM
    # but never silently replaces the one on file.
    if signals.phone and signals.phone != (lead.contact_identifier or "").strip():
        if lead.phone is None:
            lead.phone = signals.phone
            changed["phone"] = signals.phone
        elif signals.phone != lead.phone:
            changed.setdefault("phone", signals.phone)
    # Keep the highest score seen across the lead's lifetime.
    if score > 0 and (lead.lead_score is None or score > lead.lead_score):
        lead.lead_score = score
        changed["lead_score"] = f"{score:g}"
    return changed
