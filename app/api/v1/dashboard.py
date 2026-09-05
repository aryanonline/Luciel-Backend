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

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import not_found, service_unavailable, validation_error
from app.audit import log as audit
from app.channels import outbound
from app.db.session import tenant_session
from app.lifecycle import lead_erasure
from app.lifecycle.export_bundle import lead_export_rows, to_csv
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.lead import Lead, LeadState
from app.runtime import handoff
from app.runtime.orchestrator import persist_turn
from app.schemas_api.conversations import (
    AnswerEvidenceOut,
    ConversationSummaryOut,
    DeliveryDetailWire,
    EscalationEventOut,
    HumanReplyRequest,
    HumanReplyResult,
    LeadOut,
    LeadOutcomeRequest,
    MessageOut,
    PruneRequest,
    ScoringStatusWire,
    SourceChunkOut,
)

logger = logging.getLogger(__name__)

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
    """The full transcript for one session (§3.4.6 within-session memory).

    Sessions past the 90-day hot boundary (Commitment #38) have no Transcript rows —
    their transcript moved to S3 cold storage. The read-through below serves those
    on demand from the archive, so a conversation never goes blank the day its rows
    moved; only its storage tier changed."""
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
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
        if not rows and sess.archive_object_key:
            from app.lifecycle.transcript_archive import (
                ArchivedTranscriptUnavailable,
                load_archived_transcripts,
            )

            try:
                archived = await load_archived_transcripts(
                    s, admin_id=subject.admin_id, session_id=session_id
                )
            except ArchivedTranscriptUnavailable:
                # Say so. Returning [] here renders as a conversation the customer
                # never wrote anything in, which is a different — and false — claim.
                raise service_unavailable(
                    "This conversation is in long-term storage and could not be "
                    "loaded right now. Nothing has been lost; please try again shortly."
                ) from None
            return [
                MessageOut(
                    message_id=uuid.UUID(str(r["id"])),
                    role=str(r["role"]),
                    text=str(r["text"]),
                    at=datetime.fromisoformat(str(r["at"])),
                )
                for r in (archived or [])
            ]
        return [MessageOut(message_id=r.id, role=r.role, text=r.text, at=r.at) for r in rows]


@router.post("/conversations/{session_id}/messages", response_model=HumanReplyResult)
async def send_human_message(
    session_id: uuid.UUID,
    body: HumanReplyRequest,
    subject: Subject = Depends(current_admin),
) -> HumanReplyResult:
    """Admin sends a reply while holding the conversation (§3.4.12 / §11.5-11.7).

    Only valid in ``human_controlled`` mode — outside takeover Luciel owns the reply,
    and letting the admin interleave would make the transcript lie about who is
    speaking. The row is persisted with role ``human_agent`` through the SAME writer
    the loop uses, then pushed on the customer's channel.

    The widget cannot be pushed to (no outbound transport): the reply is persisted and
    the visitor receives it on the widget's next history fetch. That is reported
    honestly as ``delivered: false`` / ``deliveryDetail: "widget_poll_only"`` rather
    than claiming a send.
    """
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        if sess.mode != "human_controlled":
            raise validation_error("Take over the conversation before replying.")

        row = await persist_turn(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            session_id=session_id,
            role="human_agent",
            text=body.text,
        )
        await s.refresh(row)  # `at` is a server default — read it back before echoing it
        sess.last_message_at = row.at

        recipient = None
        if sess.resolved_lead_id is not None:
            lead = (
                await s.execute(
                    select(Lead).where(
                        Lead.admin_id == subject.admin_id, Lead.id == sess.resolved_lead_id
                    )
                )
            ).scalar_one_or_none()
            recipient = lead.contact_identifier if lead is not None else None

        result = await outbound.send_outbound_reply(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            channel=sess.channel,
            to=recipient,
            body=body.text,
        )
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=sess.instance_id,
            event_type="human_agent_message_sent",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            # No message text in the chained payload (§3.4.11 — no raw PII).
            payload={
                "session_id": str(session_id),
                "message_id": str(row.id),
                "channel": sess.channel,
                "delivered": result.delivered,
                "delivery_detail": result.detail,
            },
        )
        detail = result.detail if not result.delivered else None
        return HumanReplyResult(
            message=MessageOut(message_id=row.id, role="human_agent", text=row.text, at=row.at),
            delivered=result.delivered,
            delivery_detail=_delivery_detail(detail),
        )


# The adapter can return a provider-specific failure string (e.g. "twilio_error:...").
# The wire vocabulary is a closed set, so anything unrecognized collapses to send_failed
# — the admin learns it did not go out; the provider detail stays in the audit log.
_WIRE_DELIVERY_DETAILS = frozenset(
    {"widget_poll_only", "no_recipient", "channel_not_provisioned", "unsupported_channel"}
)


def _delivery_detail(detail: str | None) -> DeliveryDetailWire | None:
    if detail is None:
        return None
    if detail in _WIRE_DELIVERY_DETAILS:
        return cast("DeliveryDetailWire", detail)
    return "send_failed"


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

    Harmony fix 2026-08-04 (item 3) persisted the real per-turn score on
    Transcript.grounding_score (app/runtime/orchestrator.py::persist_turn) instead
    of the old hardcoded 0.50. That fix's OWN fallback path was itself dishonest,
    caught live in Harmony wave 2 (item 2c): any row with grounding_score IS NULL
    (both genuine legacy/pre-migration rows AND the harmony-fix-era test fixtures
    that seeded a "legacy" luciel row) still rendered the SAME 0.50 floor constant
    as if it had been measured -- "Grounded 0.50" for a row nobody ever scored.
    scoringStatus now makes that distinction explicit on the wire: "scored" carries
    a real groundingScore, "not_scored_legacy" carries groundingScore=null and the
    dashboard renders "Not scored (before scoring existed)", never a number.

    Harmony wave 2 (item 2a) also closes the FLAGGED CONTRACT GAP this endpoint
    used to carry: source_chunks is no longer unconditionally empty. New luciel
    replies persist real chunk refs when the retriever had them
    (app/runtime/knowledge_retrieval.py::make_chunk_attributor, threaded through
    Orchestrator.chunk_attributor -> persist_turn). attributionStatus distinguishes
    "has_sources" (non-empty, real chunks), "no_sources_retrieved" (retrieval ran,
    found nothing -- an honest empty list), and "attribution_unavailable" (we never
    captured attribution for this row, e.g. legacy rows predating this column, or a
    reply channel/path with no attribution seam bound). Empty source_chunks must
    NEVER be presented as "no source backed this answer" on its own -- that FE copy
    is only correct for "no_sources_retrieved", not for "attribution_unavailable".
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

        scored = msg.grounding_score is not None
        source_chunks_raw = msg.source_chunks
        if source_chunks_raw is None:
            attribution_status = "attribution_unavailable"
            source_chunks: list[SourceChunkOut] = []
        elif len(source_chunks_raw) == 0:
            attribution_status = "no_sources_retrieved"
            source_chunks = []
        else:
            attribution_status = "has_sources"
            source_chunks = [
                SourceChunkOut(
                    source_id=uuid.UUID(str(c["source_id"])),
                    source_name=str(c["source_name"]),
                    text=str(c["text"]),
                )
                for c in source_chunks_raw
            ]

        # #5c (live-caught 2026-08-23): a deterministic code-composed reply (greeting,
        # at-cap notice, escalation ack) is "not_applicable" — scoring never applied —
        # not "not_scored_legacy", which claims the row predates scoring. Unscored
        # rows without the 0051 marker keep the legacy status honestly.
        if scored:
            scoring_status: ScoringStatusWire = "scored"
        elif msg.deterministic:
            scoring_status = "not_applicable"
        else:
            scoring_status = "not_scored_legacy"
        return AnswerEvidenceOut(
            message_id=msg.id,
            grounding_score=msg.grounding_score if scored else None,
            scoring_status=scoring_status,
            source_chunks=source_chunks,
            attribution_status=attribution_status,
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
                email=r.email,
                intent=r.intent,
                state=r.state.value,
                outcome=r.outcome,
                last_activity_at=r.last_activity_at,
                created_at=r.created_at,
            )
            for r in rows
        ]


@router.get("/leads/export")
async def export_leads(
    fmt: str = Query(
        "csv", alias="format", description="Export format: 'csv' (default) or 'json'."
    ),
    subject: Subject = Depends(current_admin),
) -> Response:
    """Download this tenant's lead records (§3.4.10a "exportable", Legal §A7 portability).

    Leads are the Admin's own business data, and §B5 makes export one of the three tools
    the platform owes them alongside prune and the auto-prune rule — an Admin cannot be
    asked to prune what they cannot first take with them. Open formats only.

    Rows come from the same builder as the full account bundle's ``leads.csv``
    (``lead_export_rows``), so the two exports cannot drift. RLS scopes the read to the
    caller's tenant. Read-only, so no audit event is emitted here — the §5.2
    ``data_export_*`` events describe the lifecycle export BUNDLE, not a row-dump.
    """
    if fmt not in ("csv", "json"):
        raise validation_error("format must be 'csv' or 'json'.")

    async with tenant_session(subject.admin_id) as s:
        leads = (
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
        rows = lead_export_rows(leads)

    if fmt == "json":
        return Response(
            content=json.dumps(rows, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="leads.json"'},
        )
    return Response(
        content=to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


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
    """Right-to-erasure: hard-delete one lead + everything held about them
    (Architecture §3.4.11) through the ONE erasure cascade every lead-destroying
    path shares (``lifecycle.lead_erasure``). Irreversible."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        await lead_erasure.erase_lead_data(
            s,
            admin_id=subject.admin_id,
            lead=lead,
            event_type="lead_erasure",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
        )
    response.status_code = 204
    return response


@router.post("/leads/prune", status_code=204)
async def prune_leads(
    body: PruneRequest, response: Response, subject: Subject = Depends(current_admin)
) -> Response:
    """Bulk prune: permanently DELETE the named leads (§3.4.10a — prune is not a state).

    Prune differs from archive: there is no 'pruned' state, the rows are removed —
    and "removed" means the same full erasure cascade the explicit erase runs
    (audit F091: prune used to leave the person's transcripts and summaries behind
    under a dead lead id while the modal said everything was removed).
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
            await lead_erasure.erase_lead_data(
                s,
                admin_id=subject.admin_id,
                lead=lead,
                event_type="lead_pruned",
                actor_type=ActorType.admin_user,
                actor_user_id=subject.user_id,
            )
    response.status_code = 204
    return response


@router.post("/leads/{lead_id}/archive", response_model=LeadOut)
async def archive_lead(lead_id: uuid.UUID, subject: Subject = Depends(current_admin)) -> LeadOut:
    """Archive a lead (reversible state change, §3.4.10a). Keeps the row."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        lead.state = LeadState.archived
        # An owner action IS activity (§3.4.10a "no update"): the auto-prune window
        # measures from here, not from the last customer message (audit F096).
        lead.last_activity_at = datetime.now(UTC)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=lead.instance_id,
            event_type="lead_archived",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={"resolved_lead_id_hash": audit.hash_lead_id(lead_id)},
        )
        await s.flush()
        return LeadOut(
            lead_id=lead.id,
            name=lead.name,
            contact_identifier=lead.contact_identifier,
            email=lead.email,
            intent=lead.intent,
            state=lead.state.value,
            outcome=lead.outcome,
            last_activity_at=lead.last_activity_at,
            created_at=lead.created_at,
        )


@router.post("/leads/{lead_id}/outcome", response_model=LeadOut)
async def mark_lead_outcome(
    lead_id: uuid.UUID,
    body: LeadOutcomeRequest,
    subject: Subject = Depends(current_admin),
) -> LeadOut:
    """Mark a lead's business outcome (Vision §7 "where the admin marks a lead
    outcome"; C14, migration 0050). This is what conversion analytics group by;
    reversible — an owner can move a lead between in_progress/converted/lost as
    the deal actually develops. Audited without lead PII (§3.4.11)."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        old = lead.outcome
        lead.outcome = body.outcome
        # Marking an outcome is the owner working the lead — a lead they just marked
        # Won must not read as "inactive" to the auto-prune rule (audit F096).
        lead.last_activity_at = datetime.now(UTC)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=lead.instance_id,
            event_type="lead_outcome_marked",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={
                "resolved_lead_id_hash": audit.hash_lead_id(lead_id),
                "old_outcome": old,
                "new_outcome": body.outcome,
            },
        )
        await s.flush()
        # Round 5A item 3: the outcome is the most commercially meaningful fact a
        # tenant maintains, and it never reached their CRM — the deterministic
        # push now carries it through the SAME broker gates a capture push uses
        # (disabled tool / disconnected / dormant → quiet trace-recorded no-op).
        # session_id=None: a dashboard action is not a conversation; the trace
        # column is nullable for exactly this. A push failure never fails the
        # outcome save — the lead record here is already the source of truth.
        from app.runtime.crm_push import push_captured_lead  # noqa: PLC0415
        from app.runtime.lead_capture import CaptureResult, crm_lead_key  # noqa: PLC0415
        from app.tools.registry import make_tool_resolver  # noqa: PLC0415

        try:
            await push_captured_lead(
                s,
                admin_id=subject.admin_id,
                instance_id=lead.instance_id,
                captured=CaptureResult(
                    lead_id=lead.id, changed={"outcome": body.outcome}, lead_key=crm_lead_key(lead)
                ),
                lead_key=crm_lead_key(lead),
                snapshot_tool_ids=None,
                tool_resolver=make_tool_resolver(),
                session_id=None,
            )
        except Exception:  # noqa: BLE001 — the outcome save must never fail on a push
            logger.exception("lead_outcome_crm_push_failed")
        return LeadOut(
            lead_id=lead.id,
            name=lead.name,
            contact_identifier=lead.contact_identifier,
            email=lead.email,
            intent=lead.intent,
            state=lead.state.value,
            outcome=lead.outcome,
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
