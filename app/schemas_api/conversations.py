"""Dashboard conversations / leads / escalations wire shapes — mirror
schemas/conversations.ts EXACTLY (camelCase via WireModel).

Sources: Architecture §3.4.7 (summary), §3.4.9 (lead identity), §3.4.11 (erasure),
§3.4.12 (handoff/mode), §3.4.13 (answer grounding/evidence), §3.4.10a (prune vs
archive), §3.5.2 (escalation event model).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas_api.common import WireModel

ConversationModeWire = Literal["ai", "human_controlled"]
# §3.1.2 split ids, PLUS the legacy combined ``instagram_messenger`` — read-path only:
# session rows opened before the split carry it, and rewriting history would claim to
# know which platform a past conversation used. It is never offered as a configurable
# channel (the config enum in schemas_api/luciel.py does not include it).
ChannelIdWire = Literal[
    "widget",
    "email",
    "sms",
    "voice",
    "whatsapp",
    "messenger",
    "instagram",
    "instagram_messenger",
]
LeadStateWire = Literal["active", "archived"]  # NO 'pruned' — prune deletes (§3.4.10a).
MessageRoleWire = Literal["lead", "luciel", "human_agent"]
GateWire = Literal["intake", "outcome"]
# The 4 fixed signals + the server-only signals (schemas/conversations.ts).
# ``luciel_requested_handoff`` (round 7 WP-2, item 2) is Luciel's own hand_to_team
# judgment; ``reply_undeliverable`` (audit F077) is the platform's undeliverable-reply
# escalation — both are rows the escalations list serves, so they must parse here.
EscalationEventSignalWire = Literal[
    "explicit_human_request",
    "strong_negative_sentiment",
    "cannot_answer",
    "high_value_lead",
    "budget_exhausted",
    "llm_unavailable",
    "luciel_requested_handoff",
    "content_flagged",
    "reply_undeliverable",
]


class ConversationSummaryOut(WireModel):
    session_id: uuid.UUID
    lead_id: uuid.UUID | None = None
    channel: ChannelIdWire
    mode: ConversationModeWire
    started_at: datetime
    last_message_at: datetime
    summary: str | None = None
    # Round 7 WP-3b item 4: when the session ENDED (§3.4.8 — never resumed). Non-null
    # means take-over and a human reply are refused (409 ``session_ended``): nothing on
    # the customer's transport is waiting on it, so there is nothing to hold.
    ended_at: datetime | None = None
    # Round 7 WP-3b item 6: whether this session was counted against the plan's
    # conversation allowance (a ``counted_sessions`` row exists). False for a session
    # that never reached the fire-once trigger — refused at cap, an LLM outage before
    # the first model call — so the page can label an unbilled conversation instead of
    # listing it beside the billed ones as if it were one.
    counted_for_billing: bool = False


# Round 7 WP-3b item 5: what happened to a ``human_agent`` reply on the customer's
# transport, PERSISTED on the row so it replays on reopen. ``delivered`` = the
# provider accepted it; ``queued`` = the widget serves it on its next history fetch;
# ``failed`` = nothing reached the customer (``deliveryDetail`` says why). Null on
# every other role and on human replies from before the column existed — an unknown
# outcome is reported as unknown, never as delivered.
DeliveryStatusWire = Literal["delivered", "queued", "failed"]

DeliveryDetailWire = Literal[
    "widget_poll_only",
    "no_recipient",
    "channel_not_provisioned",
    "unsupported_channel",
    "send_failed",
    # 2026-09-05 audit: a voice takeover reply reaches the caller as a text (F150) —
    # labelled, never silent — or cannot because SMS is off / not attested; and the
    # senders' health gates (F070) name why nothing left.
    "voice_bridged_to_sms",
    "voice_reply_requires_sms",
    "sms_sender_not_operable",
    "email_sender_not_connected",
    "mailbox_reconnect_needed",
    # Round 7 WP-3b item 1: the lead is not known by any identifier THIS session's
    # channel can deliver to (an email-keyed record on an SMS conversation after a
    # unification). The reply is persisted, never re-routed to another channel.
    "no_recipient_for_channel",
    # Round 7 WP-3b item 7: the Meta grant that owns the surface the conversation
    # happened on is expired / errored / unbound — the owner must reconnect Meta.
    # (Used to surface as send_failed with "try again" copy.)
    "meta_reconnect_needed",
]


class MessageOut(WireModel):
    message_id: uuid.UUID
    role: MessageRoleWire
    text: str
    at: datetime
    # Round 7 WP-3b item 5 — ``human_agent`` rows only; null elsewhere. A reply Twilio
    # rejected used to read like any other message when the owner reopened the
    # conversation; the outcome now replays from the row.
    delivery_status: DeliveryStatusWire | None = None
    delivery_detail: DeliveryDetailWire | None = None
    delivered_at: datetime | None = None


class HumanReplyRequest(WireModel):
    """Admin-authored reply while the conversation is human_controlled (§3.4.12)."""

    text: str = Field(min_length=1, max_length=4000)


class HumanReplyResult(WireModel):
    """The persisted reply plus an HONEST outbound-delivery signal (§11.5-11.7).

    ``delivered`` is False whenever the reply did not reach the customer's transport.
    The widget is the expected case: it has no push channel, so the message is stored
    and the visitor sees it on the widget's next history fetch
    (``deliveryDetail == "widget_poll_only"``). The dashboard must not claim "sent" for
    a reply whose ``delivered`` is False.
    """

    message: MessageOut
    delivered: bool
    delivery_detail: DeliveryDetailWire | None = None


class SourceChunkOut(WireModel):
    source_id: uuid.UUID
    source_name: str
    text: str


# Harmony wave 2, item 2 (LIVE-VERIFIED evidence-honesty contract):
#
# scoringStatus distinguishes a REAL persisted grounding score from a row that was
# never scored at all -- "not_scored_legacy" covers BOTH pre-migration Transcript
# rows (grounding_score column did not exist yet when they were written) and any
# other luciel-role row that predates real scoring. The dashboard must render
# "Not scored (before scoring existed)" for that status, NEVER the floor constant
# as if it were a real measurement -- rendering 0.50 for an unscored row is exactly
# the "Grounded 0.50" bug this item closes. groundingScore is null whenever
# scoringStatus != "scored".
#
# "not_applicable" (#5c, live-caught 2026-08-23, migration 0051): the row is a
# DETERMINISTIC code-composed reply (persisted greeting, at-cap notice, escalation
# ack, LLM-outage ack) -- there was never a knowledge answer to score, which is a
# different truth from "predates scoring". A fresh greeting row used to serve
# not_scored_legacy and the dashboard claimed it was written "before scoring
# existed" -- false for a row minutes old. Unscored rows persisted before the
# Transcript.deterministic marker existed honestly stay not_scored_legacy (their
# origin cannot be reconstructed).
#
# attributionStatus distinguishes "the retriever found no source for this answer"
# from "we have no idea what backed this answer" (attribution simply wasn't
# captured). sourceChunks == [] must NEVER be presented as "no source backed this
# answer" -- that is a claim about retrieval, and an empty list here is frequently
# just a gap in what was captured (attribution_unavailable), not evidence of an
# ungrounded reply. FRONTEND CONTRACT CHANGE: the dashboard's per-message evidence
# panel must branch on attributionStatus, not on `sourceChunks.length === 0` alone,
# to decide which copy to render (see register note for the sibling FE task).
ScoringStatusWire = Literal["scored", "not_scored_legacy", "not_applicable"]
AttributionStatusWire = Literal[
    "has_sources",
    "no_sources_retrieved",
    "attribution_unavailable",
    # 2026-09-06 E2E walk: retrieval never ran for this turn (embedding provider
    # failed) — a fourth truth, not an empty result.
    "retrieval_unavailable",
]


class AnswerEvidenceOut(WireModel):
    message_id: uuid.UUID
    # Real score in [0,1] when scoring_status == "scored"; null otherwise. Grounding
    # floor is 0.50 uniform (§3.4.13), but this field is never backfilled with that
    # constant for a row that was not actually measured.
    grounding_score: float | None
    scoring_status: ScoringStatusWire
    source_chunks: list[SourceChunkOut]
    attribution_status: AttributionStatusWire
    flagged_by_admin: bool


class EscalationEventOut(WireModel):
    escalation_id: uuid.UUID
    session_id: uuid.UUID
    lead_id: uuid.UUID | None = None
    signal: EscalationEventSignalWire
    gate: GateWire
    fired_at: datetime
    score_or_confidence: float | None = None
    # Why it fired, in short phrases (round 6 WP-G, F124); [] for older rows.
    reasons: list[str] = Field(default_factory=list)


class LeadOut(WireModel):
    lead_id: uuid.UUID
    name: str | None = None
    contact_identifier: str | None = None
    # Round 5A item 5: the volunteered email beside a phone transport key — the
    # CRM dedupe key once known. Null when unknown or when contactIdentifier
    # already is the email.
    email: str | None = None
    # 2026-09-05 audit F111: the volunteered phone (E.164) beside a non-phone key.
    phone: str | None = None
    intent: str | None = None
    state: LeadStateWire
    # Admin-marked business outcome (Vision §7; C14). Distinct from `state`
    # (visibility lifecycle): this is what conversion analytics group by.
    outcome: Literal["in_progress", "converted", "lost"] = "in_progress"
    last_activity_at: datetime
    created_at: datetime
    # 2026-09-05 audit F134: the honest CRM state — synced / failed, with the
    # non-secret reason and the time of the last successful push. Round 7 WP-3c
    # item 5: a lead nothing ever pushed is an EXPLICIT ``never_attempted`` with
    # the reason in ``crm_detail`` (``schemas_api.leads.NeverAttemptedReason``),
    # derived from the tenant's current posture — the old null hid a lead captured
    # before the CRM connected behind the same value as "no CRM key", so the
    # dashboard could never offer the one-time backfill. null only for legacy
    # readers that predate the state.
    crm_status: Literal["synced", "failed", "never_attempted"] | None = None
    crm_detail: str | None = None
    crm_pushed_at: datetime | None = None


class LeadOutcomeRequest(WireModel):
    outcome: Literal["in_progress", "converted", "lost"]


class PruneRequest(WireModel):
    lead_ids: list[uuid.UUID]
