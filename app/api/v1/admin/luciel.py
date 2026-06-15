"""/api/v1/admin/luciel* — the Luciel instance: get/create, five pillars, lifecycle.

Frozen contract (http-admin.ts): GET returns the Luciel or JSON null (no Luciel);
PUT channels/tools wrap arrays; lifecycle posts return the updated Luciel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found
from app.audit import log as audit
from app.db.session import tenant_session
from app.lifecycle import state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import ActorType
from app.models.instance import Instance, LucielState
from app.schemas_api.luciel import (
    CreateLucielRequest,
    EscalationContactWire,
    LucielOut,
    PersonalityConfigWire,
    UpdateChannelsRequest,
    UpdateToolsRequest,
)

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
    admin_id: uuid.UUID, user_id: uuid.UUID, field: str, value: object
) -> Instance:
    async with tenant_session(admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        setattr(inst, field, value)
        await s.flush()
        await audit.emit(
            s,
            admin_id=admin_id,
            instance_id=inst.id,
            actor_user_id=user_id,
            actor_type=ActorType.admin_user,
            event_type="instance_configured",
            payload={"surface": field},
        )
        return _to_out(inst)  # type: ignore[return-value]


@router.put("/channels", response_model=LucielOut)
async def update_channels(
    req: UpdateChannelsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    channels = [c.model_dump(by_alias=True, exclude_none=True) for c in req.channels]
    return await _update_config(subject.admin_id, subject.user_id, "channels", channels)  # type: ignore[return-value]


@router.put("/tools", response_model=LucielOut)
async def update_tools(
    req: UpdateToolsRequest, subject: Subject = Depends(current_admin)
) -> LucielOut:
    tools = [t.model_dump(by_alias=True, exclude_none=True) for t in req.tools]
    return await _update_config(subject.admin_id, subject.user_id, "tools", tools)  # type: ignore[return-value]


@router.put("/escalation", response_model=LucielOut)
async def update_escalation(
    req: EscalationContactWire, subject: Subject = Depends(current_admin)
) -> LucielOut:
    return await _update_config(
        subject.admin_id,
        subject.user_id,
        "escalation",
        req.model_dump(by_alias=True, exclude_none=True),
    )  # type: ignore[return-value]


@router.put("/personality", response_model=LucielOut)
async def update_personality(
    req: PersonalityConfigWire, subject: Subject = Depends(current_admin)
) -> LucielOut:
    return await _update_config(
        subject.admin_id,
        subject.user_id,
        "personality",
        req.model_dump(by_alias=True, exclude_none=True),
    )  # type: ignore[return-value]


@router.post("/voice-consent", response_model=LucielOut)
async def acknowledge_voice_consent(subject: Subject = Depends(current_admin)) -> LucielOut:
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if inst is None:
            raise not_found("No Luciel.")
        channels = inst.channels or []
        for ch in channels:
            if ch.get("id") == "voice":
                ch["voiceConsentAcknowledgedAt"] = datetime.now(UTC).isoformat()
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
