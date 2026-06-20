"""Luciel instance + five-pillar wire shapes — mirror schemas/luciel.ts exactly.

Two families live here:
  * OUTPUT models (``*Out`` / ``LucielOut``) read JSONB back to the wire and are
    LENIENT on extra keys — the persisted blob may carry fields a given wire model
    doesn't name (e.g. a channel's ``connectionStatus``), so forbidding extras here
    would break response construction.
  * REQUEST models for the four PUT pillars set ``extra='forbid'`` (StrictWireModel).
    This is the STRUCTURAL enforcement of two locked invariants: "no model-selection
    field" and "no second Connections mechanism" (BUILD_SPEC §6; Arch §3.4.3) — any
    unknown/extra field on a pillar body is rejected at parse time → 422.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.schemas_api.common import WireModel


class StrictWireModel(WireModel):
    """Request base that rejects unknown/extra fields (BUILD_SPEC §6, Arch §3.4.3).

    extra='forbid' coexists with the camelCase alias_generator + populate_by_name on
    WireModel: aliased camelCase keys and snake_case kwargs both still validate; only
    keys matching NO declared field are rejected. Verified in tests/contract.
    """

    model_config = ConfigDict(extra="forbid")


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
    "unconfigured",
    "connected",
    "error",
    "expired",
    "revoked",
    "dormant",
    # SMS/Voice number provisioned but A2P 10DLC carrier registration not yet
    # approved — the channel is NOT live and must not be shown as connected (§3.1.6).
    "pending_carrier_registration",
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


# --- REQUEST models for the four PUT pillars (StrictWireModel: extra='forbid') ------ #
# These mirror the *Out shapes but REJECT unknown/extra fields at parse time (422).
# That structural rejection is how invariant 5 is enforced: a stray "model"/"llm" field
# (no model selection — Arch §3.4.3) or any attempt to introduce a sixth "Connections"
# mechanism (BUILD_SPEC §6) cannot be persisted because the body fails validation before
# it ever reaches _update_config.


class ChannelConfigIn(StrictWireModel):
    # channelId is constrained to the 6 frozen enum values by ChannelIdWire (Vision §3.1,
    # Arch §3.1.2); an unknown id is a 422 at parse time. The voice→consent cross-field
    # gate cannot be expressed structurally (it needs the persisted consent timestamp), so
    # it is enforced in the validators layer, not here.
    id: ChannelIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None
    voice_consent_acknowledged_at: datetime | None = None


class AddonToolIn(StrictWireModel):
    # AddonToolIdWire pins the 8 frozen addon ids; the 4 builtin cognition items
    # (capture_leads/escalate/handoff/summarize) are NOT in that Literal, so naming one
    # is already a 422 — but the validators layer also rejects them explicitly with a
    # clear message (Vision §3.2, Arch §3.8.7: cognition is non-interactive, never an addon).
    id: AddonToolIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None


class EscalationRoutingRuleIn(StrictWireModel):
    # signal is locked to the 4 FIXED escalation signals by EscalationSignalWire
    # (Vision §3.4, Arch §3.4.5): a routing rule may only reference one of the four;
    # an unknown/added signal is rejected at parse time.
    signal: EscalationSignalWire
    channel: NotificationChannelWire
    cc_owner_email: bool | None = None


class PersonalityAxesIn(StrictWireModel):
    # Each axis is bounded 0..1 (Vision §3.5, Arch §3.4.3); out-of-range is a 422.
    tone: float = Field(ge=0, le=1)
    verbosity: float = Field(ge=0, le=1)
    formality: float = Field(ge=0, le=1)
    pace: float = Field(ge=0, le=1)


# Update payloads (PUT bodies wrap arrays as { channels: [...] } / { tools: [...] }).
# mock-admin.ts replaces the whole array/object (clone(channels) etc.), so semantics are
# FULL-REPLACE: validate the entire body, then overwrite — no partial merge.
class UpdateChannelsRequest(StrictWireModel):
    channels: list[ChannelConfigIn]


class UpdateToolsRequest(StrictWireModel):
    tools: list[AddonToolIn]


class UpdateEscalationRequest(StrictWireModel):
    # Only who/how is editable — contacts + per-signal routing channel + ccOwner. The set
    # of signals themselves is NOT a field here (it is fixed in code), so an admin cannot
    # add/remove/disable a signal (Vision §3.4, Arch §3.4.5). routing references are
    # validated against the 4 fixed signals by EscalationRoutingRuleIn.
    primary_email: str | None = None
    primary_sms: str | None = None
    secondary_email: str | None = None
    secondary_sms: str | None = None
    preferred_channel: NotificationChannelWire | None = None
    routing: list[EscalationRoutingRuleIn] | None = None


class UpdatePersonalityRequest(StrictWireModel):
    # NO model-selection field may EVER be accepted (Arch §3.4.3): extra='forbid' means a
    # "model"/"llm"/"engine" key is a 422. businessContext ≤ 280 (Vision §3.5). The
    # custom-preset-requires-4-axes cross-field rule is enforced in the validators layer.
    preset: PersonalityPresetWire
    axes: PersonalityAxesIn | None = None
    business_context: str | None = Field(default=None, max_length=280)
