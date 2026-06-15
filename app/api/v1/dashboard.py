"""/api/v1/dashboard/* — control-plane conversation + lead surfaces.

Mirrors the frozen admin client (http-admin.ts: conversations.* and leads.*). Every
route is gated by ``current_admin`` (verified session, §3.7.2) and runs inside a
tenant_session so the three-layer isolation holds (§3.7.2b). Paths are authoritative
against the frozen contract — message evidence / flag are NESTED under the message.

Sources: Architecture §3.4.7 (summary list), §3.4.12 (take-over / hand-back),
§3.4.13 (answer grounding/evidence + admin flag), §3.5.2 (escalation event model),
§3.4.9 (lead identity), §3.4.10a (prune = delete vs archive), §3.4.11 (erasure).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import not_found
from app.audit import log as audit
from app.db.session import tenant_session
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.lead import Lead, LeadState
from app.runtime import handoff
from app.schemas_api.conversations import (
    AnswerEvidenceOut,
    ConversationSummaryOut,
    EscalationEventOut,
    LeadOut,
    MessageOut,
    PruneRequest,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


# --- conversations ------------------------------------------------------------ #
@router.get("/conversations", response_model=list[ConversationSummaryOut])
async def list_conversations(
    subject: Subject = Depends(current_admin),
) -> list[ConversationSummaryOut]:
    """The dashboard conversation list (§3.4.7) — newest activity first."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(SessionModel)
                    .where(SessionModel.admin_id == subject.admin_id)
                    .order_by(SessionModel.last_message_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            ConversationSummaryOut(
                session_id=r.id,
                lead_id=r.resolved_lead_id,
                channel=r.channel,
                mode=r.mode,
                started_at=r.started_at,
                last_message_at=r.last_message_at,
                summary=r.summary,
            )
            for r in rows
        ]


async def _require_session(
    s: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> SessionModel:
    sess: SessionModel | None = (
        await s.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        raise not_found("Conversation not found.")
    return sess


@router.get("/conversations/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> list[MessageOut]:
    """The full transcript for one session (§3.4.6 within-session memory)."""
    async with tenant_session(subject.admin_id) as s:
        await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        rows = (
            (
                await s.execute(
                    select(Transcript)
                    .where(
                        Transcript.admin_id == subject.admin_id,
                        Transcript.session_id == session_id,
                    )
                    .order_by(Transcript.at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [MessageOut(message_id=r.id, role=r.role, text=r.text, at=r.at) for r in rows]


@router.post("/conversations/{session_id}/take-over", response_model=ConversationSummaryOut)
async def take_over(
    session_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> ConversationSummaryOut:
    """Admin live takeover → human_controlled (§3.4.12). Stops the loop for the session."""
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        sess = await handoff.take_over(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            session_id=session_id,
            actor_user_id=subject.user_id,
            luciel_initiated=False,
        )
        return _summary(sess)


@router.post("/conversations/{session_id}/hand-back", response_model=ConversationSummaryOut)
async def hand_back(
    session_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> ConversationSummaryOut:
    """Admin hands control back to Luciel: human_controlled → ai (§3.4.12)."""
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        sess = await handoff.hand_back(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            session_id=session_id,
            actor_user_id=subject.user_id,
        )
        return _summary(sess)


@router.get(
    "/conversations/{session_id}/messages/{message_id}/evidence",
    response_model=AnswerEvidenceOut,
)
async def get_answer_evidence(
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    subject: Subject = Depends(current_admin),
) -> AnswerEvidenceOut:
    """Answer grounding + source chunks for one Luciel message (§3.4.13).

    FLAGGED CONTRACT GAP: per-message grounding scores + retrieved source chunks are
    persisted by the vector-retrieval arc (Arc 11/16). Arc 14 persists the transcript
    but not the per-message evidence rows, so this returns the message at the uniform
    grounding floor (0.50, §3.4.13) with no chunks until the retrieval store lands.
    The wire shape is frozen now so the dashboard binds without later change.
    """
    async with tenant_session(subject.admin_id) as s:
        await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        msg = (
            await s.execute(
                select(Transcript).where(
                    Transcript.admin_id == subject.admin_id,
                    Transcript.session_id == session_id,
                    Transcript.id == message_id,
                )
            )
        ).scalar_one_or_none()
        if msg is None:
            raise not_found("Message not found.")
        return AnswerEvidenceOut(
            message_id=msg.id,
            grounding_score=0.50,  # uniform grounding floor (§3.4.13) until Arc 11/16.
            source_chunks=[],
            flagged_by_admin=False,
        )


@router.post("/conversations/{session_id}/messages/{message_id}/flag", status_code=204)
async def flag_answer(
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    response: Response,
    subject: Subject = Depends(current_admin),
) -> Response:
    """Admin flags a Luciel answer for review (§3.4.13). Audit-only in Arc 14."""
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        msg = (
            await s.execute(
                select(Transcript).where(
                    Transcript.admin_id == subject.admin_id,
                    Transcript.session_id == session_id,
                    Transcript.id == message_id,
                )
            )
        ).scalar_one_or_none()
        if msg is None:
            raise not_found("Message not found.")
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            event_type="answer_flagged_by_admin",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={"session_id": str(session_id), "message_id": str(message_id)},
        )
    response.status_code = 204
    return response


@router.get("/escalations", response_model=list[EscalationEventOut])
async def list_escalations(
    subject: Subject = Depends(current_admin),
) -> list[EscalationEventOut]:
    """All escalation events for the tenant, newest first (§3.5.2)."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(EscalationEvent)
                    .where(EscalationEvent.admin_id == subject.admin_id)
                    .order_by(EscalationEvent.fired_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            EscalationEventOut(
                escalation_id=r.escalation_id,
                session_id=r.session_id,
                lead_id=r.resolved_lead_id,
                signal=r.signal,
                gate=r.gate,
                fired_at=r.fired_at,
                score_or_confidence=r.score_or_confidence,
            )
            for r in rows
        ]


# --- leads -------------------------------------------------------------------- #
@router.get("/leads", response_model=list[LeadOut])
async def list_leads(subject: Subject = Depends(current_admin)) -> list[LeadOut]:
    """Lead list (§3.4.9). Newest activity first."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(Lead)
                    .where(Lead.admin_id == subject.admin_id)
                    .order_by(Lead.last_activity_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            LeadOut(
                lead_id=r.id,
                name=r.name,
                contact_identifier=r.contact_identifier,
                intent=r.intent,
                state=r.state.value,
                last_activity_at=r.last_activity_at,
                created_at=r.created_at,
            )
            for r in rows
        ]


async def _require_lead(s: AsyncSession, *, admin_id: uuid.UUID, lead_id: uuid.UUID) -> Lead:
    lead: Lead | None = (
        await s.execute(select(Lead).where(Lead.admin_id == admin_id, Lead.id == lead_id))
    ).scalar_one_or_none()
    if lead is None:
        raise not_found("Lead not found.")
    return lead


@router.delete("/leads/{lead_id}", status_code=204)
async def erase_lead(
    lead_id: uuid.UUID, response: Response, subject: Subject = Depends(current_admin)
) -> Response:
    """Right-to-erasure: hard-delete one lead + audit (§3.4.11). Irreversible."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        instance_id = lead.instance_id
        await s.delete(lead)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            event_type="lead_erasure",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            # No raw PII in the payload — lead id only (§3.4.11).
            payload={"lead_id": str(lead_id)},
        )
    response.status_code = 204
    return response


@router.post("/leads/prune", status_code=204)
async def prune_leads(
    body: PruneRequest, response: Response, subject: Subject = Depends(current_admin)
) -> Response:
    """Bulk prune: permanently DELETE the named leads (§3.4.10a — prune is not a state).

    Prune differs from archive: there is no 'pruned' state, the rows are removed.
    """
    async with tenant_session(subject.admin_id) as s:
        for lead_id in body.lead_ids:
            lead = (
                await s.execute(
                    select(Lead).where(Lead.admin_id == subject.admin_id, Lead.id == lead_id)
                )
            ).scalar_one_or_none()
            if lead is None:
                continue  # idempotent — already gone.
            instance_id = lead.instance_id
            await s.delete(lead)
            await audit.emit(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                event_type="lead_pruned",
                actor_type=ActorType.admin_user,
                actor_user_id=subject.user_id,
                payload={"lead_id": str(lead_id)},
            )
    response.status_code = 204
    return response


@router.post("/leads/{lead_id}/archive", response_model=LeadOut)
async def archive_lead(lead_id: uuid.UUID, subject: Subject = Depends(current_admin)) -> LeadOut:
    """Archive a lead (reversible state change, §3.4.10a). Keeps the row."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        lead.state = LeadState.archived
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=lead.instance_id,
            event_type="lead_archived",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={"lead_id": str(lead_id)},
        )
        await s.flush()
        return LeadOut(
            lead_id=lead.id,
            name=lead.name,
            contact_identifier=lead.contact_identifier,
            intent=lead.intent,
            state=lead.state.value,
            last_activity_at=lead.last_activity_at,
            created_at=lead.created_at,
        )


def _summary(sess: SessionModel) -> ConversationSummaryOut:
    return ConversationSummaryOut(
        session_id=sess.id,
        lead_id=sess.resolved_lead_id,
        channel=sess.channel,
        mode=sess.mode,
        started_at=sess.started_at,
        last_message_at=sess.last_message_at,
        summary=sess.summary,
    )
