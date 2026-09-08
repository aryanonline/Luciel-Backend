"""/api/v1/admin/luciel* — the Luciel instance: get/create, five pillars, lifecycle.

Frozen contract (http-admin.ts): GET returns the Luciel or JSON null (no Luciel);
PUT channels/tools wrap arrays; lifecycle posts return the updated Luciel.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found, validation_error
from app.api.v1.admin.luciel_invariants import (
    ensure_config_editable,
    validate_channels,
    validate_personality,
)
from app.audit import log as audit
from app.connections import gate
from app.db.session import tenant_session
from app.employee.status import build_employee_status
from app.lifecycle import state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import ActorType
from app.models.instance import Instance, LucielState
from app.schemas_api.employee import EmployeeStatusOut
from app.schemas_api.luciel import (
    CapabilityGroupOut,
    ChannelConfigIn,
    CreateLucielRequest,
    LucielOut,
    ResendContactConfirmationRequest,
    TestSmsRequest,
    TestSmsResult,
    UpdateAllowedOriginsRequest,
    UpdateBusinessNameRequest,
    UpdateChannelsRequest,
    UpdateDailyBriefRequest,
    UpdateEscalationRequest,
    UpdateLeadRetentionRequest,
    UpdatePersonalityRequest,
    UpdateTeamAvailabilityRequest,
    UpdateToolsRequest,
)
from app.tools import catalog
from app.tools.authorization import sync_tool_authorizations

router = APIRouter(prefix="/api/v1/admin/luciel", tags=["luciel"])

DEFAULT_CHANNELS = [
    {"id": "widget", "enabled": True},
    {"id": "email", "enabled": False},
    {"id": "sms", "enabled": False, "connectionStatus": "unconfigured"},
    {"id": "voice", "enabled": False, "connectionStatus": "unconfigured"},
    {"id": "whatsapp", "enabled": False},
    {"id": "messenger", "enabled": False},
    {"id": "instagram", "enabled": False},
]
# Derived from the tool catalog so a newly registered addon tool cannot be missing from a
# freshly created Luciel — default-off, per §3.3.3 default-deny.
DEFAULT_TOOLS = [{"id": tool_id, "enabled": False} for tool_id in catalog.ALL_TOOL_IDS]


def _merge_live_channel_status(
    channels: list[dict[str, object]], live_status: dict[str, str]
) -> list[dict[str, object]]:
    """Overlay LIVE connection status onto the stored channels JSONB (bug fix).

    The channels pillar's persisted ``connectionStatus`` was previously written ONLY
    by a client PUT echo — nothing on the backend ever synced it from the actual
    ``instance_connections`` row an attach/OAuth-callback updates (§3.8.4). That made
    a freshly BYO'd Twilio number (or any other channel-backing connection) invisible
    to the dashboard: the card kept showing whatever stale/default status it was
    created with and asked the owner to reconnect a channel that was never actually
    disconnected. This is the read-side fix: every response that carries ``channels``
    is overlaid with the live status for channels that have a backing connection
    (§gate.channel_connection_statuses); channels with no backing connection (widget)
    are left exactly as stored."""
    return [
        {**ch, "connectionStatus": live_status[str(ch["id"])]}
        if str(ch.get("id")) in live_status
        else dict(ch)
        for ch in channels
    ]


async def _contact_health(
    s: AsyncSession, admin_id: uuid.UUID, escalation: dict[str, object] | None
) -> list[dict[str, object]]:
    """Deliverability truth for the configured email contacts (round 5B #13)."""
    from app.models.email_incident import is_suppressed, is_verified  # noqa: PLC0415
    from app.notifications.delivery_incidents import states_for  # noqa: PLC0415

    escalation = escalation or {}
    addresses = [
        str(a) for a in (escalation.get("primaryEmail"), escalation.get("secondaryEmail")) if a
    ]
    if not addresses:
        return []
    states = await states_for(s, admin_id, addresses)
    out: list[dict[str, object]] = []
    for address in addresses:
        row = states.get(address.strip().lower())
        if is_suppressed(row):
            state = "bouncing"
        elif is_verified(row):
            state = "verified"
        elif row is not None and row.verification_pending_jti:
            state = "pending_confirmation"
        else:
            state = "unverified"
        out.append(
            {
                "address": address,
                "state": state,
                "verifiedAt": row.verified_at.isoformat() if row and row.verified_at else None,
                "lastBouncedAt": (
                    row.hard_bounced_at.isoformat() if row and row.hard_bounced_at else None
                ),
            }
        )
    return out


# 2026-09-05 audit F151: every tool that needs a connection reports that connection's
# LIVE status on the wire, so the Tools pillar can say "connected" for a send_sms
# whose number really sends, instead of "finish setting that up" forever.
_TOOL_CONNECTION_TYPES: dict[str, str] = {
    tool_id: connection_type
    for tool_id, connection_type in catalog.TOOL_CONNECTION_TYPE.items()
    if connection_type
}


async def _tool_statuses(
    s: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> dict[str, str]:
    by_type: dict[str, str] = {}
    for connection_type in sorted(set(_TOOL_CONNECTION_TYPES.values())):
        status = await gate.connection_status(
            s, admin_id=admin_id, instance_id=instance_id, connection_type=connection_type
        )
        by_type[connection_type] = status or "unconfigured"
    return {tool_id: by_type[ct] for tool_id, ct in _TOOL_CONNECTION_TYPES.items()}


def _merge_tool_status(
    tools: list[dict[str, object]], statuses: dict[str, str]
) -> list[dict[str, object]]:
    return [
        {**t, "connectionStatus": statuses.get(str(t.get("id")), t.get("connectionStatus"))}
        for t in tools
    ]


def _to_out(
    inst: Instance,
    *,
    live_channel_status: dict[str, str] | None = None,
    contact_health: list[dict[str, object]] | None = None,
    tool_status: dict[str, str] | None = None,
) -> LucielOut:
    channels = inst.channels or []
    if live_channel_status:
        channels = _merge_live_channel_status(channels, live_channel_status)
    tools = list(inst.tools or [])
    if tool_status:
        tools = _merge_tool_status(tools, tool_status)
    return LucielOut.model_validate(
        {
            "instanceId": inst.id,
            "name": inst.name,
            "websiteUrl": inst.website_url,
            "state": inst.state.value,
            "embedKeyPublicId": inst.embed_key_public_id,
            "channels": channels,
            "tools": tools,
            "escalation": inst.escalation or {},
            "escalationContactHealth": contact_health or [],
            "personality": inst.personality or {"preset": "warm_concierge"},
            "leadRetentionDays": inst.lead_retention_days,
            "allowedOrigins": list(inst.allowed_origins) if inst.allowed_origins else None,
            "timezone": inst.timezone,
            "teamAvailability": inst.team_availability or None,
            "businessShortName": inst.business_short_name,
            "dailyBriefEnabled": bool(inst.daily_brief_enabled),
            "graceWindowStartedAt": (
                inst.grace_window_started_at.isoformat() if inst.grace_window_started_at else None
            ),
        }
    )


async def _load(admin_id: uuid.UUID) -> Instance | None:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()


@router.get("")
async def get_luciel(subject: Subject = Depends(current_admin)) -> LucielOut | None:
    inst = await _load(subject.admin_id)
    # Contract: returns JSON null (200) when the account is empty — NOT 404.
    if inst is None:
        return None
    async with tenant_session(subject.admin_id) as s:
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        tool_status = await _tool_statuses(s, subject.admin_id, inst.id)
        health = await _contact_health(s, subject.admin_id, inst.escalation)
    return _to_out(
        inst, live_channel_status=live_status, contact_health=health, tool_status=tool_status
    )


@router.get("/capabilities", response_model=list[CapabilityGroupOut])
async def list_capabilities(
    subject: Subject = Depends(current_admin),
) -> list[CapabilityGroupOut]:
    """The owner-facing capability groups the tools roll up into (Decision #8).

    Scheduling is ONE capability over the SINGLE calendar connection — read-before-write
    is an internal mechanic, not two toggles, and moving or cancelling a booking rides the
    same connection that made it. Serving the grouping means the UI renders one control
    from server truth instead of hardcoding which tool ids belong together, so adding a
    tool to a capability stays a catalog change.

    Static catalog data, so no tenant session is needed; admin-authed like /providers
    because the shape of what we offer is not public.
    """
    del subject
    return [
        CapabilityGroupOut(
            capability=spec.capability,
            label=spec.label,
            help_text=spec.help_text,
            tool_ids=list(spec.tool_ids),
            connection_type=spec.connection_type,
        )
        for spec in catalog.CAPABILITIES
    ]


@router.post("", response_model=LucielOut)
async def create_luciel(
    req: CreateLucielRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        existing = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if existing is not None:
            raise conflict("A Luciel already exists for this account.")
        inst = Instance(
            id=uuid.uuid4(),
            admin_id=subject.admin_id,
            name=req.name,
            website_url=req.website_url,
            business_one_liner=req.business_one_liner,
            state=LucielState.active,
            embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:12]}",
            channels=DEFAULT_CHANNELS,
            tools=DEFAULT_TOOLS,
            escalation={},
            personality={"preset": "warm_concierge"},
        )
        s.add(inst)
        await s.flush()
        # First Luciel completes first-run.
        from app.models.instance import Account

        acct = (await s.execute(select(Account).where(Account.id == subject.admin_id))).scalar_one()
        acct.has_completed_first_run = True
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="instance_created",
        )
        return _to_out(inst)


async def _update_config(
    admin_id: uuid.UUID,
    user_id: uuid.UUID,
    field: str,
    build_value: Callable[[Instance], object],
    after: Callable[[AsyncSession, Instance], Awaitable[None]] | None = None,
) -> LucielOut:
    """Validated full-replace of one pillar's JSONB blob.

    Replaces the prior blind ``setattr``: now we (1) load the instance, (2) gate on
    lifecycle state (Arch §3.6), then (3) call ``build_value`` — which runs the
    state-dependent invariant checks against the loaded ``inst`` and returns the value
    to persist. Only after all checks pass do we overwrite (full-replace, matching the
    frozen mock-admin.ts semantics) and emit the audit event.

    ``after`` runs inside the same transaction for pillars whose config also drives a
    relational table (tools → ``instance_tool_authorizations``), so the JSONB blob and
    the table it gates can never be written apart.
    """
    async with tenant_session(admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        # Lifecycle gate before any mutation (Arch §3.6): no edits once grace/hard-deleted.
        ensure_config_editable(inst)
        value = build_value(inst)
        setattr(inst, field, value)
        await s.flush()
        if after is not None:
            await after(s, inst)
        await audit.emit(
            s,
            admin_id=admin_id,
            instance_id=inst.id,
            actor_user_id=user_id,
            actor_type=ActorType.admin_user,
            event_type="instance_configured",
            payload={"surface": field},
        )
        live_status = (
            await gate.channel_connection_statuses(s, admin_id=admin_id, instance_id=inst.id)
            if field == "channels"
            else None
        )
        tool_status = await _tool_statuses(s, admin_id, inst.id)
        health = await _contact_health(s, admin_id, inst.escalation)
        return _to_out(
            inst, live_channel_status=live_status, contact_health=health, tool_status=tool_status
        )


@router.put("/channels", response_model=LucielOut)
async def update_channels(
    req: UpdateChannelsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    prior_enabled: dict[str, bool] = {}

    def _build(inst: Instance) -> object:
        # Voice-enable requires prior consent-ack (Vision §3.1, Arch §3.1.2).
        validate_channels(req, inst)
        prior_enabled.update(
            {
                str(c.get("id")): bool(c.get("enabled"))
                for c in (inst.channels or [])
                if isinstance(c, dict)
            }
        )
        return _stamp_voice_consent_ack(_stamp_sms_compliance_ack(req.channels, inst), inst)

    async def _after(s: AsyncSession, inst: Instance) -> None:
        # Round 6 WP-D (audit F163): the dependent send tool goes off WITH its channel,
        # server-side and in the same transaction — the client used to issue a second
        # PUT, which a closed tab or a failed request could skip, leaving Send SMS on
        # over a channel that no longer exists.
        turned_off = [
            channel
            for channel, was_on in prior_enabled.items()
            if was_on
            and not any(
                isinstance(c, dict) and c.get("id") == channel and c.get("enabled")
                for c in (inst.channels or [])
            )
        ]
        await _cascade_channel_tools(s, subject.admin_id, inst, turned_off)

    return await _update_config(subject.admin_id, subject.user_id, "channels", _build, after=_after)


#: The send tool that cannot exist without its channel (Arch §3.3).
CHANNEL_DEPENDENT_TOOL: dict[str, str] = {"email": "send_email", "sms": "send_sms"}


async def _cascade_channel_tools(
    s: AsyncSession, admin_id: uuid.UUID, inst: Instance, channels_off: list[str]
) -> None:
    """Disable the dependent tool of each channel in ``channels_off`` with the reason on
    it, and keep the broker's authorization table in step (WP-D, F163)."""
    targets = {
        CHANNEL_DEPENDENT_TOOL[c]: f"{c}_channel_disabled"
        for c in channels_off
        if c in CHANNEL_DEPENDENT_TOOL
    }
    if not targets:
        return
    changed = False
    tools: list[dict[str, object]] = []
    for t in inst.tools or []:
        if not isinstance(t, dict):
            continue
        tool_id = str(t.get("id"))
        if tool_id in targets and t.get("enabled"):
            tools.append({**t, "enabled": False, "disabledReason": targets[tool_id]})
            changed = True
        else:
            tools.append(dict(t))
    if not changed:
        return
    inst.tools = tools
    await s.flush()
    from app.schemas_api.luciel import AddonToolIn  # noqa: PLC0415

    await sync_tool_authorizations(
        s,
        admin_id=admin_id,
        instance_id=inst.id,
        tools=[AddonToolIn.model_validate(t) for t in tools],
    )


SMS_ACK_FIELD = "smsComplianceAcknowledgedAt"
VOICE_ACK_FIELD = "voiceConsentAcknowledgedAt"


def _stamp_voice_consent_ack(
    channels: list[dict[str, object]], inst: Instance
) -> list[dict[str, object]]:
    """A persisted voice consent ack survives every channels PUT (Legal §3.1.2).

    PUT /channels is a full replace, so a client that omitted the field silently
    ERASED the one-time recording/AI-disclosure consent record — after which voice
    could never be re-enabled without re-consenting, and the audit trail of the
    original acceptance was gone (latent, live-caught 2026-08-18). Mirror of the
    SMS stamper: an existing stamp is always re-carried and never overwritten by a
    client value; a NEW inline ack on an enabling write is still accepted (that is
    the validate_channels inline-ack path)."""
    prior_stamp = next(
        (
            c.get(VOICE_ACK_FIELD)
            for c in (inst.channels or [])
            if isinstance(c, dict) and c.get("id") == "voice"
        ),
        None,
    )
    if not prior_stamp:
        return channels
    for ch in channels:
        if ch.get("id") == "voice":
            ch[VOICE_ACK_FIELD] = prior_stamp
    return channels


def _stamp_sms_compliance_ack(
    channels: list[ChannelConfigIn], inst: Instance
) -> list[dict[str, object]]:
    """Persist the A2P 10DLC / sender-of-record / STOP-HELP ack instant (Legal §A2, §A6).

    Enabling SMS is only reachable through the acknowledgment modal, so the first write
    that turns SMS on IS the acknowledgment — recording the timestamp here makes it
    durable across sessions instead of living in client state. The value is server-owned:
    whatever the client sent is dropped, an existing stamp is never overwritten (the ack
    happened once), and it is never cleared by a later disable, so the audit trail of
    "they accepted the carrier obligations" survives SMS being toggled off.
    """
    prior = {c.get("id"): c for c in (inst.channels or [])}
    now = datetime.now(UTC).isoformat()
    out: list[dict[str, object]] = []
    for ch in channels:
        dumped = ch.model_dump(by_alias=True, exclude_none=True)
        if ch.id == "sms":
            dumped.pop(SMS_ACK_FIELD, None)
            stamp = prior.get("sms", {}).get(SMS_ACK_FIELD) or (now if ch.enabled else None)
            if stamp:
                dumped[SMS_ACK_FIELD] = stamp
        out.append(dumped)
    return out


@router.put("/tools", response_model=LucielOut)
async def update_tools(
    req: UpdateToolsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    # AddonToolIdWire (Vision §3.2, Arch §3.8.7) already rejects unknown ids AND the 4
    # builtin cognition items at parse time, so no extra value-check is needed here.
    # The SECOND GATE (a tool is usable only when enabled AND its connection is healthy)
    # is RUNTIME, not config — so we intentionally persist enabled=true even if the
    # connection is unhealthy (Arch §3.8.7); the runtime arbiter enforces usability.
    def _build(_: Instance) -> object:
        return [t.model_dump(by_alias=True, exclude_none=True) for t in req.tools]

    # The JSONB blob is the config surface the dashboard reads back; the broker's
    # default-deny gate 1 reads instance_tool_authorizations (Arch §3.3.3). Both must
    # move together or an admin toggling a tool on still gets `not_authorized` at
    # dispatch — write them in the same transaction so they can never diverge.
    return await _update_config(
        subject.admin_id,
        subject.user_id,
        "tools",
        _build,
        after=lambda s, inst: sync_tool_authorizations(
            s, admin_id=subject.admin_id, instance_id=inst.id, tools=req.tools
        ),
    )


async def _kick_contact_verification(
    s: AsyncSession, admin_id: uuid.UUID, addresses: list[str]
) -> None:
    """Start the confirm loop for each configured email contact not yet verified
    (round 5B item 13). Same-transaction with the pillar write; an address
    already verified (or already pending) is not re-mailed on a re-save."""
    from app.auth.tokens import mint_link_token  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415
    from app.notifications import email_dispatcher  # noqa: PLC0415
    from app.notifications.delivery_incidents import begin_verification, states_for  # noqa: PLC0415

    settings = get_settings()
    states = await states_for(s, admin_id, addresses)
    for address in addresses:
        state = states.get(address.strip().lower())
        if state is not None and (state.verified_at or state.verification_pending_jti):
            continue
        jti = uuid.uuid4().hex
        token = mint_link_token(
            admin_id,
            "escalation_contact",
            settings.escalation_contact_link_ttl_seconds,
            jti,
        )
        await begin_verification(s, admin_id=admin_id, email=address, jti=jti)
        await email_dispatcher.send_escalation_contact_confirm(
            s,
            admin_id=admin_id,
            to=address,
            confirm_url=f"{settings.app_base_url}/escalation-contact/confirm?token={token}",
        )


@router.put("/escalation", response_model=LucielOut)
async def update_escalation(
    req: UpdateEscalationRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    # The 4 escalation signals are FIXED and never admin-configurable (Vision §3.4, Arch
    # §3.4.5): UpdateEscalationRequest has no "signals" field, and routing rules are
    # constrained to the 4 fixed signals by EscalationSignalWire — so signal
    # add/remove/disable or an unknown signal is already a 422 at parse time. Only the
    # who/how (contacts + per-signal routing channel + ccOwner) is editable here.
    def _build(_: Instance) -> object:
        return req.model_dump(by_alias=True, exclude_none=True)

    async def _after(s: AsyncSession, _inst: Instance) -> None:
        # Round 5B item 13: saving a contact starts its confirmation loop.
        await _kick_contact_verification(
            s,
            subject.admin_id,
            [a for a in (req.primary_email, req.secondary_email) if a],
        )

    return await _update_config(
        subject.admin_id, subject.user_id, "escalation", _build, after=_after
    )


@router.post("/escalation/test-sms", response_model=TestSmsResult)
async def send_test_escalation_sms(
    req: TestSmsRequest, subject: Subject = Depends(current_admin)
) -> TestSmsResult:
    """Send a test text to a configured SMS escalation contact (2026-09-05 audit F084).

    Email contacts confirm themselves through a link; a phone number had no loop at
    all — the first real hot lead was the test. The text goes out through the tenant's
    own number (BYO), so it also proves the number can send. Rate-limited per tenant
    so it cannot be turned into a texting tool."""
    from app.channels import rate_limit  # noqa: PLC0415
    from app.channels.outbound import send_outbound_reply  # noqa: PLC0415

    decision = rate_limit.get_store().check_source("escalation_test_sms", str(subject.admin_id))
    if not decision.allowed:
        from app.api.errors import rate_limited  # noqa: PLC0415

        raise rate_limited("Too many test texts. Please wait a minute.", retry_after_seconds=60)
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        cfg = inst.escalation if isinstance(inst.escalation, dict) else {}
        number = cfg.get("primarySms" if req.which == "primary" else "secondarySms")
        if not isinstance(number, str) or not number:
            raise validation_error("No SMS contact is saved in that slot yet.")
        result = await send_outbound_reply(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            channel="sms",
            to=number,
            body=(
                "VantageMind test: escalation texts from your Luciel will arrive at this "
                "number. No action needed."
            ),
        )
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="escalation_contact_verification_sent",
            payload={
                "channel": "sms",
                "slot": req.which,
                "number_suffix": number[-4:],
                "delivered": result.delivered,
                "detail": result.detail,
            },
        )
        return TestSmsResult(delivered=result.delivered, detail=result.detail)


@router.post("/escalation/resend-confirmation", response_model=LucielOut)
async def resend_contact_confirmation(
    req: ResendContactConfirmationRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """Re-send the confirmation link for a currently configured contact (round 5B
    item 13). Re-minting invalidates the previous link; a 5-minute cooldown keeps
    this from becoming a send-spam vector."""
    from datetime import timedelta  # noqa: PLC0415

    from app.auth.tokens import mint_link_token  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415
    from app.notifications import email_dispatcher  # noqa: PLC0415
    from app.notifications.delivery_incidents import begin_verification, states_for  # noqa: PLC0415

    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        escalation = inst.escalation or {}
        configured = {
            str(a).strip().lower()
            for a in (escalation.get("primaryEmail"), escalation.get("secondaryEmail"))
            if a
        }
        address = req.address.strip().lower()
        if address not in configured:
            raise validation_error(
                "That address is not currently configured as an escalation contact."
            )
        settings = get_settings()
        states = await states_for(s, subject.admin_id, [address])
        state = states.get(address)
        if state is not None and state.verification_sent_at is not None:
            age = datetime.now(UTC) - state.verification_sent_at
            if age < timedelta(minutes=5):
                raise validation_error(
                    "A confirmation was sent moments ago — give it a few minutes, then retry."
                )
        jti = uuid.uuid4().hex
        token = mint_link_token(
            subject.admin_id,
            "escalation_contact",
            settings.escalation_contact_link_ttl_seconds,
            jti,
        )
        await begin_verification(s, admin_id=subject.admin_id, email=address, jti=jti)
        await email_dispatcher.send_escalation_contact_confirm(
            s,
            admin_id=subject.admin_id,
            to=address,
            confirm_url=f"{settings.app_base_url}/escalation-contact/confirm?token={token}",
        )
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        tool_status = await _tool_statuses(s, subject.admin_id, inst.id)
        health = await _contact_health(s, subject.admin_id, inst.escalation)
        return _to_out(
            inst, live_channel_status=live_status, contact_health=health, tool_status=tool_status
        )


@router.put("/lead-retention", response_model=LucielOut)
async def update_lead_retention(
    req: UpdateLeadRetentionRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """Set (or clear) the Admin's opt-in lead auto-prune rule (§3.4.10a, Legal §B5).

    ``leadRetentionDays: null`` turns the rule off. Turning it on is the Admin's own
    retention choice over their own business data: it is reversible as a setting, but the
    deletions the sweep then performs are permanent.
    """

    def _build(_: Instance) -> object:
        return req.lead_retention_days

    return await _update_config(subject.admin_id, subject.user_id, "lead_retention_days", _build)


@router.put("/allowed-origins", response_model=LucielOut)
async def update_allowed_origins(
    req: UpdateAllowedOriginsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """Name the websites the widget may load on (round 6 WP-I, audit F066).

    The embed key is public by design; this list is the owner's only way to say WHERE
    it may be used. The widget data plane refuses every other page origin with a 403.
    An empty list turns the restriction off. The dashboard's own preview always
    works — the platform origin is never refused."""

    def _build(_: Instance) -> object:
        return list(req.origins) or None

    return await _update_config(subject.admin_id, subject.user_id, "allowed_origins", _build)


@router.put("/team-availability", response_model=LucielOut)
async def update_team_availability(
    req: UpdateTeamAvailabilityRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """Describe when the HUMAN team is reachable (round 6 WP-E, audit F054).

    Luciel has no hours and keeps answering around the clock whatever is set here. The
    description is used for two things only: honest follow-up wording in a hand-off
    ("someone will follow up tomorrow at 9:00 am") and routing an after-hours
    escalation to a different contact. ``enabled: false`` turns both effects off."""

    def _build(inst: Instance) -> object:
        # The timezone rides on its own column — the daily brief reads it even with
        # the setting off — written in the same transaction and audit event.
        inst.timezone = req.timezone
        blob = req.model_dump(by_alias=True, exclude_none=True)
        blob.pop("timezone", None)
        return blob

    return await _update_config(subject.admin_id, subject.user_id, "team_availability", _build)


@router.get("/status")
async def employee_status(subject: Subject = Depends(current_admin)) -> EmployeeStatusOut | None:
    """What Luciel can do right now, needs from the owner, and did (round 6 WP-F).

    Derived from rows that already exist — never narrated. ``onDuty`` is a constant:
    Luciel has no hours. JSON null when the account has no Luciel (same as GET)."""
    inst = await _load(subject.admin_id)
    if inst is None:
        return None
    async with tenant_session(subject.admin_id) as s:
        status = await build_employee_status(s, admin_id=subject.admin_id, instance_id=inst.id)
    if status is None:
        return None
    return EmployeeStatusOut.model_validate(asdict(status))


@router.put("/daily-brief", response_model=LucielOut)
async def update_daily_brief(
    req: UpdateDailyBriefRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """Turn the morning brief on or off (round 6 WP-F). On by default; sent once per
    local day at 07:00 in the owner's timezone (UTC until one is set)."""

    def _build(_: Instance) -> object:
        return bool(req.enabled)

    return await _update_config(subject.admin_id, subject.user_id, "daily_brief_enabled", _build)


@router.put("/business-name", response_model=LucielOut)
async def update_business_name(
    req: UpdateBusinessNameRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    """The short name Luciel calls the business (round 6 WP-F) — greetings and the
    AI-identity disclosure say "an AI assistant for Acme" instead of "this business".
    Never the Luciel's own name. Empty clears it."""

    def _build(_: Instance) -> object:
        return req.business_short_name

    return await _update_config(subject.admin_id, subject.user_id, "business_short_name", _build)


@router.put("/personality", response_model=LucielOut)
async def update_personality(
    req: UpdatePersonalityRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    # NO model-selection field is accepted (Arch §3.4.3) — enforced structurally by
    # extra='forbid' on UpdatePersonalityRequest. Custom preset requires the 4 axes.
    def _build(_: Instance) -> object:
        validate_personality(req)
        # Non-custom presets ignore axes per doc (preset is the source of the shape):
        # drop any stray axes so they are never persisted for a non-custom preset.
        dumped = req.model_dump(by_alias=True, exclude_none=True)
        if req.preset != "custom":
            dumped.pop("axes", None)
        return dumped

    return await _update_config(subject.admin_id, subject.user_id, "personality", _build)


@router.post("/rotate-embed-key", response_model=LucielOut)
async def rotate_embed_key(subject: Subject = Depends(current_admin)) -> LucielOut:
    """Mint a fresh widget embed key (round 5B item 17). A leaked key's only
    remedy used to be deleting and recreating the whole Luciel. Rotation is
    IMMEDIATE and breaking on purpose: the old key stops resolving the moment
    this returns, and every embedded site must update its snippet — the response
    (and the Embed page) carries the new one. Audited with both key ids (embed
    keys are public identifiers, not secrets)."""
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        ensure_config_editable(inst)
        old_key = inst.embed_key_public_id
        inst.embed_key_public_id = f"vm_live_{uuid.uuid4().hex[:12]}"
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="embed_key_rotated",
            payload={"old_embed_key": old_key, "new_embed_key": inst.embed_key_public_id},
        )
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        return _to_out(inst, live_channel_status=live_status)


@router.post("/voice-consent", response_model=LucielOut)
async def acknowledge_voice_consent(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        # Same lifecycle gate as the PUT pillars (Arch §3.6): no consent-ack on a
        # grace-window/hard-deleted instance.
        ensure_config_editable(inst)
        # Rebuild as a NEW list so SQLAlchemy detects the JSONB change (the column is not
        # a MutableList, so in-place edits of the existing list are NOT flushed).
        now = datetime.now(UTC).isoformat()
        channels = [
            {**ch, "voiceConsentAcknowledgedAt": now} if ch.get("id") == "voice" else dict(ch)
            for ch in (inst.channels or [])
        ]
        inst.channels = channels
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="voice_consent_ack",
        )
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        return _to_out(inst, live_channel_status=live_status)


@router.delete("/voice-consent", response_model=LucielOut)
async def withdraw_voice_consent(subject: Subject = Depends(current_admin)) -> LucielOut:
    """Withdraw the one-time voice recording/AI-disclosure consent (round 6 WP-D).

    Voice is switched off in the same write — it cannot run without the consent —
    and turning it back on asks for the acknowledgement again. Audited."""
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        ensure_config_editable(inst)
        channels = []
        for ch in inst.channels or []:
            if ch.get("id") == "voice":
                cleared = {k: v for k, v in ch.items() if k != VOICE_ACK_FIELD}
                cleared["enabled"] = False
                channels.append(cleared)
            else:
                channels.append(dict(ch))
        inst.channels = channels
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="voice_consent_withdrawn",
        )
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        tool_status = await _tool_statuses(s, subject.admin_id, inst.id)
        return _to_out(inst, live_channel_status=live_status, tool_status=tool_status)


@router.delete("/sms-compliance-ack", response_model=LucielOut)
async def withdraw_sms_compliance_ack(subject: Subject = Depends(current_admin)) -> LucielOut:
    """Withdraw the A2P 10DLC / sender-of-record / STOP-HELP acknowledgement (WP-D).

    SMS is switched off in the same write and Send SMS goes with it (F163 cascade);
    enabling SMS again re-asks for the acknowledgement. Audited."""
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        ensure_config_editable(inst)
        channels = []
        was_on = False
        for ch in inst.channels or []:
            if ch.get("id") == "sms":
                was_on = bool(ch.get("enabled"))
                cleared = {k: v for k, v in ch.items() if k != SMS_ACK_FIELD}
                cleared["enabled"] = False
                channels.append(cleared)
            else:
                channels.append(dict(ch))
        inst.channels = channels
        await s.flush()
        await _cascade_channel_tools(s, subject.admin_id, inst, ["sms"] if was_on else [])
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="sms_compliance_ack_withdrawn",
        )
        live_status = await gate.channel_connection_statuses(
            s, admin_id=subject.admin_id, instance_id=inst.id
        )
        tool_status = await _tool_statuses(s, subject.admin_id, inst.id)
        return _to_out(inst, live_channel_status=live_status, tool_status=tool_status)


# --- lifecycle -------------------------------------------------------------- #
def _lifecycle_error_to_http(exc: LifecycleError) -> None:
    code = str(exc)
    if code == "no_luciel":
        raise not_found("No Luciel.")
    raise conflict(f"Illegal lifecycle transition: {code}")


@router.post("/pause", response_model=LucielOut)
async def pause(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        try:
            inst = await state_machine.pause(s, subject.admin_id, subject.user_id)
        except LifecycleError as exc:
            _lifecycle_error_to_http(exc)
        return _to_out(inst)


@router.post("/resume", response_model=LucielOut)
async def resume(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        try:
            inst = await state_machine.resume(s, subject.admin_id, subject.user_id)
        except LifecycleError as exc:
            _lifecycle_error_to_http(exc)
        return _to_out(inst)


@router.post("/delete", response_model=LucielOut)
async def delete(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        try:
            inst = await state_machine.delete_luciel(s, subject.admin_id, subject.user_id)
        except LifecycleError as exc:
            _lifecycle_error_to_http(exc)
        return _to_out(inst)


@router.post("/restore", response_model=LucielOut)
async def restore(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        try:
            inst = await state_machine.restore(s, subject.admin_id, subject.user_id)
        except LifecycleError as exc:
            _lifecycle_error_to_http(exc)
        return _to_out(inst)
