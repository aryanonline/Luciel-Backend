"""Luciel instance + five-pillar wire shapes — mirror schemas/luciel.ts exactly."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas_api.common import WireModel

LucielStateWire = Literal["active", "paused", "luciel_grace_window", "luciel_hard_deleted"]
ChannelIdWire = Literal["widget", "email", "sms", "voice", "whatsapp", "instagram_messenger"]
AddonToolIdWire = Literal[
    "check_availability",
    "book_appointment",
    "send_email",
    "send_sms",
    "lookup_record",
    "schedule_callback",
    "push_to_crm",
    "bring_your_own_webhook",
]
ConnectionStatusWire = Literal[
    "unconfigured", "connected", "error", "expired", "revoked", "dormant"
]
KnowledgeSyncStatusWire = Literal["synced", "syncing", "needs_reconnect"]
NotificationChannelWire = Literal["email", "sms"]
EscalationSignalWire = Literal[
    "explicit_human_request", "strong_negative_sentiment", "cannot_answer", "high_value_lead"
]
PersonalityPresetWire = Literal[
    "warm_concierge", "professional_advisor", "friendly_expert", "trusted_authority", "custom"
]


class ChannelConfigOut(WireModel):
    id: ChannelIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None
    voice_consent_acknowledged_at: datetime | None = None


class AddonToolOut(WireModel):
    id: AddonToolIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None


class EscalationRoutingRule(WireModel):
    signal: EscalationSignalWire
    channel: NotificationChannelWire
    cc_owner_email: bool | None = None


class EscalationContactWire(WireModel):
    primary_email: str | None = None
    primary_sms: str | None = None
    secondary_email: str | None = None
    secondary_sms: str | None = None
    preferred_channel: NotificationChannelWire | None = None
    routing: list[EscalationRoutingRule] | None = None


class PersonalityAxes(WireModel):
    tone: float = Field(ge=0, le=1)
    verbosity: float = Field(ge=0, le=1)
    formality: float = Field(ge=0, le=1)
    pace: float = Field(ge=0, le=1)


class PersonalityConfigWire(WireModel):
    preset: PersonalityPresetWire
    axes: PersonalityAxes | None = None
    business_context: str | None = Field(default=None, max_length=280)


class LucielOut(WireModel):
    instance_id: uuid.UUID
    name: str
    website_url: str
    state: LucielStateWire
    embed_key_public_id: str | None = None
    channels: list[ChannelConfigOut]
    tools: list[AddonToolOut]
    escalation: EscalationContactWire
    personality: PersonalityConfigWire
    grace_window_started_at: datetime | None = None


class CreateLucielRequest(WireModel):
    name: str = Field(min_length=1)
    website_url: str = Field(min_length=1)
    business_one_liner: str = Field(min_length=1, max_length=280)


# Update payloads (PUT bodies wrap arrays as { channels: [...] } / { tools: [...] }).
class UpdateChannelsRequest(WireModel):
    channels: list[ChannelConfigOut]


class UpdateToolsRequest(WireModel):
    tools: list[AddonToolOut]
