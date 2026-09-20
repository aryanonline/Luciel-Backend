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


class MessageOut(WireModel):
    message_id: uuid.UUID
    role: MessageRoleWire
    text: str
    at: datetime


class HumanReplyRequest(WireModel):
    """Admin-authored reply while the conversation is human_controlled (§3.4.12)."""

    text: str = Field(min_length=1, max_length=4000)


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
]


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
    # 2026-09-05 audit F134: the honest CRM state. null = never attempted (no CRM,
    # tool off, or no key the CRM accepts); synced / failed otherwise, with the
    # non-secret reason and the time of the last successful push.
    crm_status: Literal["synced", "failed"] | None = None
    crm_detail: str | None = None
    crm_pushed_at: datetime | None = None


class LeadOutcomeRequest(WireModel):
    outcome: Literal["in_progress", "converted", "lost"]


class PruneRequest(WireModel):
    lead_ids: list[uuid.UUID]
