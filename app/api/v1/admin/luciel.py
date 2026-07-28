"""/api/v1/admin/luciel* — the Luciel instance: get/create, five pillars, lifecycle.

Frozen contract (http-admin.ts): GET returns the Luciel or JSON null (no Luciel);
PUT channels/tools wrap arrays; lifecycle posts return the updated Luciel.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found
from app.api.v1.admin.luciel_invariants import (
    ensure_config_editable,
    validate_channels,
    validate_personality,
)
from app.audit import log as audit
from app.db.session import tenant_session
from app.lifecycle import state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import ActorType
from app.models.instance import Instance, LucielState
from app.schemas_api.luciel import (
    ChannelConfigIn,
    CreateLucielRequest,
    LucielOut,
    UpdateChannelsRequest,
    UpdateEscalationRequest,
    UpdatePersonalityRequest,
    UpdateToolsRequest,
)
from app.tools.authorization import sync_tool_authorizations

router = APIRouter(prefix="/api/v1/admin/luciel", tags=["luciel"])

DEFAULT_CHANNELS = [
    {"id": "widget", "enabled": True},
    {"id": "email", "enabled": False},
    {"id": "sms", "enabled": False, "connectionStatus": "unconfigured"},
    {"id": "voice", "enabled": False, "connectionStatus": "unconfigured"},
    {"id": "whatsapp", "enabled": False},
    {"id": "instagram_messenger", "enabled": False},
]
DEFAULT_TOOLS = [
    {"id": "check_availability", "enabled": False},
    {"id": "book_appointment", "enabled": False},
    {"id": "send_email", "enabled": False},
    {"id": "send_sms", "enabled": False},
    {"id": "lookup_record", "enabled": False},
    {"id": "schedule_callback", "enabled": False},
    {"id": "push_to_crm", "enabled": False},
    {"id": "bring_your_own_webhook", "enabled": False},
]


def _to_out(inst: Instance) -> LucielOut:
    return LucielOut.model_validate(
        {
            "instanceId": inst.id,
            "name": inst.name,
            "websiteUrl": inst.website_url,
            "state": inst.state.value,
            "embedKeyPublicId": inst.embed_key_public_id,
            "channels": inst.channels or [],
            "tools": inst.tools or [],
            "escalation": inst.escalation or {},
            "personality": inst.personality or {"preset": "warm_concierge"},
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
    return _to_out(inst) if inst is not None else None


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
        return _to_out(inst)


@router.put("/channels", response_model=LucielOut)
async def update_channels(
    req: UpdateChannelsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    def _build(inst: Instance) -> object:
        # Voice-enable requires prior consent-ack (Vision §3.1, Arch §3.1.2).
        validate_channels(req, inst)
        return _stamp_sms_compliance_ack(req.channels, inst)

    return await _update_config(subject.admin_id, subject.user_id, "channels", _build)


SMS_ACK_FIELD = "smsComplianceAcknowledgedAt"


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

    return await _update_config(subject.admin_id, subject.user_id, "escalation", _build)


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
        return _to_out(inst)


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
