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

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, EmailStr, Field, field_validator

from app.channels.origins import MAX_ALLOWED_ORIGINS, normalize_origin
from app.schemas_api.common import WireModel

# International format (country code first, 8–15 digits) — the only shape Twilio can
# deliver an escalation text to (2026-09-05 audit F084).
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class StrictWireModel(WireModel):
    """Request base that rejects unknown/extra fields (BUILD_SPEC §6, Arch §3.4.3).

    extra='forbid' coexists with the camelCase alias_generator + populate_by_name on
    WireModel: aliased camelCase keys and snake_case kwargs both still validate; only
    keys matching NO declared field are rejected. Verified in tests/contract.
    """

    model_config = ConfigDict(extra="forbid")


LucielStateWire = Literal["active", "paused", "luciel_grace_window", "luciel_hard_deleted"]
# §3.1.2: the wire enums carry SEPARATE whatsapp/messenger/instagram channels. One
# `meta` connection powers WhatsApp + Messenger; Instagram rides its own Business Login
# connection — but each is its own channel row, toggled and destination-bound on its own.
ChannelIdWire = Literal["widget", "email", "sms", "voice", "whatsapp", "messenger", "instagram"]
AddonToolIdWire = Literal[
    "check_availability",
    "book_appointment",
    # Scheduling is ONE capability over the SINGLE calendar connection (Decision #8):
    # offering a time and moving/cancelling that time ride the same connection, so these
    # connect once with check_availability/book_appointment and are gated together.
    "reschedule_appointment",
    "cancel_appointment",
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
    # The owner handed the connection back (Decision #5b): the stored credential is
    # destroyed and the row is in a clean, RECONNECTABLE state. Distinct from `revoked`
    # (terminal, account teardown) and from `unconfigured` (never connected).
    "not_connected",
    # SMS/Voice number provisioned but A2P 10DLC carrier registration not yet
    # approved — the channel is NOT live and must not be shown as connected (§3.1.6).
    "pending_carrier_registration",
    # Own-domain email address chosen but inbound DNS/MX routing not yet verified —
    # NOT live; shown as "Action needed: complete email routing" (§3.1.6a, Decision
    # #49). Subdomain mode skips this and is immediately `connected`.
    "pending_email_routing",
    # The designated BYO SMS/Voice number is not hosted in the tenant's own
    # Twilio account (§3.1.4 operability probe) — distinct from a pending
    # carrier registration: the number needs hosting/porting first. Previously
    # degraded to `error` on the wire, which hid the actionable guidance.
    "not_operable_hosting_required",
]
# §3.2.3: paused_reconnect_needed (was needs_reconnect) + error (Axis4-F02).
KnowledgeSyncStatusWire = Literal["synced", "syncing", "paused_reconnect_needed", "error"]
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
    # A2P 10DLC / sender-of-record / STOP-HELP ack (Legal §A2, §A6). Server-stamped when
    # SMS is first enabled; read-only on the wire.
    sms_compliance_acknowledged_at: datetime | None = None


class AddonToolOut(WireModel):
    id: AddonToolIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None
    # Set by the platform when a tool was switched off as a CONSEQUENCE of something else
    # — today, its connection being disconnected (Decision #5b). The owner is told why
    # scheduling went away instead of finding a silently-off toggle. Server-derived.
    disabled_reason: str | None = None


class CapabilityGroupOut(WireModel):
    """One owner-facing capability that several tool ids implement (Decision #8).

    Scheduling is ONE capability over the SINGLE calendar connection: read-before-write
    is an internal mechanic, so the owner sees "Appointment scheduling", not four
    toggles. The group is served rather than hardcoded in the UI so adding a tool to a
    capability stays a backend catalog change.
    """

    capability: str
    label: str
    help_text: str
    tool_ids: list[AddonToolIdWire]
    # The single connection type the whole group is gated on — disconnecting it disables
    # every tool in the group (Decision #5b cascade). null for a group whose tools need no
    # external connection.
    connection_type: str | None = None


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
    # The runtime already reads assistantName for the AI-identity disclosure (§3.4.16);
    # declaring it makes it settable instead of always falling back to
    # "<business>'s assistant".
    assistant_name: str | None = Field(default=None, max_length=80)


class EscalationContactHealthOut(WireModel):
    """Per-address deliverability/verification state (round 5B item 13).

    Served as a SIBLING of the escalation blob, never inside it — the pillar PUT
    is a strict full-replace the frontend echoes back, and server-owned state
    inside it would be wiped or 422 on every save."""

    address: str
    # unverified (no confirmation ever) | pending_confirmation | verified | bouncing
    state: str
    verified_at: datetime | None = None
    last_bounced_at: datetime | None = None


class LucielOut(WireModel):
    instance_id: uuid.UUID
    name: str
    website_url: str
    state: LucielStateWire
    embed_key_public_id: str | None = None
    channels: list[ChannelConfigOut]
    tools: list[AddonToolOut]
    escalation: EscalationContactWire
    # Round 5B item 13: deliverability truth for the configured email contacts.
    escalation_contact_health: list[EscalationContactHealthOut] = Field(default_factory=list)
    personality: PersonalityConfigWire
    # Opt-in lead auto-prune window in days; null means the rule is off (§3.4.10a).
    lead_retention_days: int | None = None
    # The website origins the widget may load on; null means any page (round 6 WP-I).
    allowed_origins: list[str] | None = None
    grace_window_started_at: datetime | None = None


class ResendContactConfirmationRequest(WireModel):
    """POST /admin/luciel/escalation/resend-confirmation body (round 5B item 13)."""

    address: str = Field(min_length=3, max_length=320)


class ConfirmEscalationContactRequest(WireModel):
    """POST /api/v1/escalation-contact/confirm body — token-authorized, public
    (the contact may not be the account owner and has no session)."""

    token: str = Field(min_length=8)


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
    # Accepted so the client can echo back the value it read from GET without a 422
    # (StrictWireModel forbids extras), but the endpoint DISCARDS it: the ack instant is
    # server-owned, so an admin cannot backdate or forge their A2P acknowledgment.
    sms_compliance_acknowledged_at: datetime | None = None


class AddonToolIn(StrictWireModel):
    # AddonToolIdWire pins the frozen addon ids; the 4 builtin cognition items
    # (capture_leads/escalate/handoff/summarize) are NOT in that Literal, so naming one
    # is already a 422 — but the validators layer also rejects them explicitly with a
    # clear message (Vision §3.2, Arch §3.8.7: cognition is non-interactive, never an addon).
    id: AddonToolIdWire
    enabled: bool
    connection_status: ConnectionStatusWire | None = None
    # Accepted (and ignored) so a client can PUT back a tool list it just read. The
    # platform owns this field; an admin re-enabling the tool simply clears it.
    disabled_reason: str | None = None


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


class TestSmsRequest(StrictWireModel):
    """POST /admin/luciel/escalation/test-sms — which saved SMS contact to text."""

    which: Literal["primary", "secondary"]


class TestSmsResult(WireModel):
    """Outcome of a test text: whether the tenant's own number could deliver it."""

    delivered: bool
    detail: str | None = None


class UpdateEscalationRequest(StrictWireModel):
    # Only who/how is editable — contacts + per-signal routing channel + ccOwner. The set
    # of signals themselves is NOT a field here (it is fixed in code), so an admin cannot
    # add/remove/disable a signal (Vision §3.4, Arch §3.4.5). routing references are
    # validated against the 4 fixed signals by EscalationRoutingRuleIn.
    # EmailStr (round 5B item 13): a malformed contact is a 422 at save time —
    # SES would happily "accept" it and the hot lead would vanish. IN model
    # only; the read model stays `str` so a previously stored malformed value
    # cannot crash the read/routing path.
    primary_email: EmailStr | None = None
    primary_sms: str | None = None
    secondary_email: EmailStr | None = None
    secondary_sms: str | None = None
    preferred_channel: NotificationChannelWire | None = None
    routing: list[EscalationRoutingRuleIn] | None = None

    # E.164 at save time (2026-09-05 audit F084): a contact number that could never
    # receive a text was accepted as-is and the first real escalation was the detector.
    # Spaces, dashes, dots and brackets are stripped; the stored form is +digits.
    @field_validator("primary_sms", "secondary_sms", mode="before")
    @classmethod
    def _e164(cls, value: object) -> object:
        if value is None or value == "":
            return None
        raw = str(value)
        # Formatting is forgiven; anything else (letters, an "x123" extension) is
        # refused — an extension cannot receive a text.
        cleaned = "".join(ch for ch in raw if ch not in " -.()\t")
        if not _E164.match(cleaned):
            raise ValueError(
                "Enter the number in international format, e.g. +16045551234 "
                "(country code first, digits only)."
            )
        return cleaned


class UpdatePersonalityRequest(StrictWireModel):
    # NO model-selection field may EVER be accepted (Arch §3.4.3): extra='forbid' means a
    # "model"/"llm"/"engine" key is a 422. businessContext ≤ 280 (Vision §3.5). The
    # custom-preset-requires-4-axes cross-field rule is enforced in the validators layer.
    preset: PersonalityPresetWire
    axes: PersonalityAxesIn | None = None
    business_context: str | None = Field(default=None, max_length=280)
    assistant_name: str | None = Field(default=None, max_length=80)


class UpdateAllowedOriginsRequest(StrictWireModel):
    """PUT /admin/luciel/allowed-origins — the websites the widget may load on.

    Each entry is normalised to ``scheme://host[:port]`` (round 6 WP-I, F066); a path,
    query, credentials or a non-http(s) scheme is refused with the fix in the reason,
    because an origin with a path would silently never match and the owner's own site
    would be refused. An empty list turns the restriction OFF (any page)."""

    origins: list[str] = Field(default_factory=list, max_length=MAX_ALLOWED_ORIGINS)

    @field_validator("origins")
    @classmethod
    def _normalized(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            origin = normalize_origin(raw)
            if origin is None:
                raise ValueError(
                    f"'{raw}' is not a website address — use the form https://www.example.com "
                    "(no path or query)."
                )
            if origin not in out:
                out.append(origin)
        return out


class UpdateLeadRetentionRequest(StrictWireModel):
    # The Admin's own lead auto-prune rule (§3.4.10a). null turns it OFF, which is the
    # default and the only value the platform ever assumes on the Admin's behalf. A
    # positive window is required when on: 0 would mean "delete every lead the moment it
    # stops being active", which no Admin means to ask for.
    lead_retention_days: int | None = Field(default=None, ge=1)
