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

from app.schemas_api.common import WireModel

ConversationModeWire = Literal["ai", "human_controlled"]
ChannelIdWire = Literal["widget", "email", "sms", "voice", "whatsapp", "instagram_messenger"]
LeadStateWire = Literal["active", "archived"]  # NO 'pruned' — prune deletes (§3.4.10a).
MessageRoleWire = Literal["lead", "luciel", "human_agent"]
GateWire = Literal["intake", "outcome"]
# The 4 fixed signals + the 2 server-only signals (schemas/conversations.ts).
EscalationEventSignalWire = Literal[
    "explicit_human_request",
    "strong_negative_sentiment",
    "cannot_answer",
    "high_value_lead",
    "budget_exhausted",
    "llm_unavailable",
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


class SourceChunkOut(WireModel):
    source_id: uuid.UUID
    source_name: str
    text: str


class AnswerEvidenceOut(WireModel):
    message_id: uuid.UUID
    grounding_score: float  # [0,1]; grounding floor is 0.50 uniform (§3.4.13).
    source_chunks: list[SourceChunkOut]
    flagged_by_admin: bool


class EscalationEventOut(WireModel):
    escalation_id: uuid.UUID
    session_id: uuid.UUID
    lead_id: uuid.UUID | None = None
    signal: EscalationEventSignalWire
    gate: GateWire
    fired_at: datetime
    score_or_confidence: float | None = None


class LeadOut(WireModel):
    lead_id: uuid.UUID
    name: str | None = None
    contact_identifier: str | None = None
    intent: str | None = None
    state: LeadStateWire
    last_activity_at: datetime
    created_at: datetime


class PruneRequest(WireModel):
    lead_ids: list[uuid.UUID]
