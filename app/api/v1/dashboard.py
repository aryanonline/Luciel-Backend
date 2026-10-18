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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import ApiError, conflict, not_found, service_unavailable, validation_error
from app.audit import log as audit
from app.channels import outbound
from app.db.session import tenant_session
from app.lifecycle import lead_erasure
from app.lifecycle.export_bundle import lead_export_rows, to_csv
from app.models.audit import ActorType
from app.models.budget import CountedSession
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance
from app.models.lead import Lead, LeadState
from app.runtime import handoff
from app.runtime.knowledge_retrieval import is_retrieval_unavailable
from app.runtime.orchestrator import persist_turn
from app.schemas_api.conversations import (
    AnswerEvidenceOut,
    ConversationSummaryOut,
    DeliveryDetailWire,
    DeliveryStatusWire,
    EscalationEventOut,
    HumanReplyRequest,
    HumanReplyResult,
    LeadOut,
    LeadOutcomeRequest,
    MessageOut,
    MessageRoleWire,
    PruneRequest,
    ScoringStatusWire,
    SourceChunkOut,
)
from app.schemas_api.leads import CrmBackfillResult, LeadPatchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


# --- conversations ------------------------------------------------------------ #
# 2026-09-05 audit WP7: every list endpoint is bounded. The defaults keep the
# existing clients whole (they never asked for more than a page fits); the cap
# stops one tenant's history from being one unbounded query.
PAGE_DEFAULT = 200
PAGE_MAX = 500


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, PAGE_MAX)), max(0, offset)


@router.get("/conversations", response_model=list[ConversationSummaryOut])
async def list_conversations(
    limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[ConversationSummaryOut]:
    """The dashboard conversation list (§3.4.7) — newest activity first, paged.

    Each row carries ``countedForBilling`` (round 7 WP-3b, item 6): whether the
    session reached the fire-once budget trigger. The ledger is joined here rather
    than re-derived from mode or message count, because the ledger IS the billing
    truth (§3.4.1b) — a session refused at cap or lost to an outage before its first
    model call has no row and must not be listed as if it were billed.
    """
    limit, offset = _page(limit, offset)
    async with tenant_session(subject.admin_id) as s:
        rows = (
            await s.execute(
                select(SessionModel, CountedSession.id)
                .outerjoin(CountedSession, _counted_join(subject.admin_id))
                .where(SessionModel.admin_id == subject.admin_id)
                .order_by(SessionModel.last_message_at.desc(), SessionModel.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return [_summary(sess, counted=counted_id is not None) for sess, counted_id in rows]


def _counted_join(admin_id: uuid.UUID) -> ColumnElement[bool]:
    """ON clause pairing a session with its ``counted_sessions`` row — at most one,
    by ``uq_counted_session_once`` (admin, instance, session), so the join never
    multiplies rows."""
    return and_(
        CountedSession.admin_id == admin_id,
        CountedSession.instance_id == SessionModel.instance_id,
        CountedSession.session_id == SessionModel.id,
    )


async def _counted_for_billing(
    s: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    row = (
        await s.execute(
            select(CountedSession.id).where(
                CountedSession.admin_id == admin_id, CountedSession.session_id == session_id
            )
        )
    ).first()
    return row is not None


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
                ArchivedTranscriptAbsent,
                ArchivedTranscriptUnavailable,
                load_archived_transcripts,
            )

            try:
                archived = await load_archived_transcripts(
                    s, admin_id=subject.admin_id, session_id=session_id
                )
            except ArchivedTranscriptUnavailable as exc:
                # Say so — and say WHICH. Returning [] renders as a conversation the
                # customer never wrote anything in (a false claim), and "nothing has
                # been lost" is only true when the store failed to ANSWER, not when it
                # answered that the object is gone (round 7 WP-5, item 7).
                if isinstance(exc, ArchivedTranscriptAbsent):
                    raise not_found(
                        "This conversation's transcript is no longer available in "
                        "long-term storage."
                    ) from None
                raise service_unavailable(
                    "This conversation is in long-term storage and could not be "
                    "read right now. Nothing has been lost; please try again shortly."
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
        return [_message_out(r) for r in rows]


def _message_out(row: Transcript) -> MessageOut:
    """One transcript row on the wire, with a human reply's PERSISTED delivery outcome
    (round 7 WP-3b, item 5) so a rejected reply still reads as failed on reopen. The
    stored detail may be a provider string; the wire vocabulary is closed, so it maps
    through the same collapse the live result did (send_failed for anything else)."""
    status = row.delivery_status
    return MessageOut(
        message_id=row.id,
        role=cast("MessageRoleWire", row.role),
        text=row.text,
        at=row.at,
        delivery_status=cast("DeliveryStatusWire", status) if status else None,
        delivery_detail=_delivery_detail(row.delivery_detail) if status else None,
        delivered_at=row.delivered_at,
    )


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
        if sess.ended_at is not None:
            # Round 7 WP-3b item 4: the conversation ENDED (§3.4.8). Nothing on the
            # customer's transport is waiting on it; a reply persisted here would sit in
            # a transcript nobody is reading and, on a push channel, arrive as a message
            # out of nowhere. Refused before anything is written.
            raise _session_ended()
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

        recipient = await _takeover_recipient(s, admin_id=subject.admin_id, sess=sess)

        if recipient is None and sess.channel not in outbound.POLL_ONLY_CHANNELS:
            # The lead is not known by any identifier THIS channel can deliver to
            # (round 7 WP-3b, item 1). The reply stays persisted and the owner is told
            # so; it is never re-routed to an identifier on another channel.
            result = outbound.OutboundResult(
                channel=sess.channel, delivered=False, detail="no_recipient_for_channel"
            )
        elif sess.channel == "voice":
            # A voice conversation has no text transport of its own: the reply reaches
            # the caller as an SMS on the tenant's number (owner decision 2026-09-05).
            # Honest on both sides — gated on the SMS channel being switched on and the
            # number being connected/attested, and LABELLED as a text so the owner never
            # believes the caller heard it on the call (audit F150).
            result = await _bridge_voice_reply_to_sms(
                s, admin_id=subject.admin_id, sess=sess, to=recipient, body=body.text
            )
        else:
            result = await outbound.send_outbound_reply(
                s,
                admin_id=subject.admin_id,
                instance_id=sess.instance_id,
                channel=sess.channel,
                to=recipient,
                body=body.text,
                # The reply goes out AS the conversation it answers (round 7 WP-3b): in
                # the customer's own email thread (subject + the inbound message id the
                # ingress persisted — Graph's /messages/{id}/reply on a BYO mailbox,
                # In-Reply-To on SES), and on Meta AS the surface they wrote to.
                subject=sess.inbound_subject,
                in_reply_to=sess.inbound_message_id,
                sender_id=sess.meta_destination_id,
                # A person wrote this: signed by the business, never as an AI (item 3).
                author_role=outbound.AUTHOR_HUMAN_AGENT,
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
        # A delivered reply may still carry a detail the owner must see (the voice→SMS
        # bridge); an undelivered one always does.
        detail = result.detail if (not result.delivered or result.detail) else None
        # Round 7 WP-3b item 5: the outcome lives on the row, not only in this response
        # and the audit log — so GET .../messages replays a rejected reply as failed
        # when the owner reopens the conversation instead of listing it like any other
        # message. The provider's own reason is kept (truncated to the column) and
        # collapses to the wire vocabulary on the way out, as it does here.
        _persist_delivery_outcome(row, result)
        return HumanReplyResult(
            message=_message_out(row),
            delivered=result.delivered,
            delivery_detail=_delivery_detail(detail),
        )


def _persist_delivery_outcome(row: Transcript, result: outbound.OutboundResult) -> None:
    if result.delivered:
        row.delivery_status = "delivered"
        row.delivered_at = datetime.now(UTC)
    elif result.detail == "widget_poll_only":
        # Not a failure: the widget has no push transport and serves the reply on its
        # next history fetch. Not a delivery either — nothing has reached the visitor
        # yet, so delivered_at stays NULL.
        row.delivery_status = "queued"
    else:
        row.delivery_status = "failed"
    row.delivery_detail = result.detail[:64] if result.detail else None


def _session_ended() -> ApiError:
    """The typed refusal for a takeover action on an ENDED session (round 7 WP-3b,
    item 4): 409 → ``conflict`` on the frontend's status map, with the wire word
    ``session_ended`` in the message so the page can name the state rather than
    show a generic conflict."""
    return conflict(
        "This conversation has ended, so there is nothing to take over or reply "
        "into (session_ended)."
    )


async def _session_lead(s: AsyncSession, *, admin_id: uuid.UUID, sess: SessionModel) -> Lead | None:
    if sess.resolved_lead_id is None:
        return None
    lead: Lead | None = (
        await s.execute(
            select(Lead).where(Lead.admin_id == admin_id, Lead.id == sess.resolved_lead_id)
        )
    ).scalar_one_or_none()
    return lead


async def _takeover_recipient(
    s: AsyncSession, *, admin_id: uuid.UUID, sess: SessionModel
) -> str | None:
    """The identifier a human reply on THIS session's channel can reach (round 7
    WP-3b, item 1), or None when the conversation's own recipient cannot be told.

    ``Lead.contact_identifier`` is whichever identity the record was keyed on FIRST —
    after a widget-then-SMS unification that is the older, email-keyed row — so it is
    not, in general, an identifier the session's transport can deliver to; the reply
    used to go to it regardless of channel. Nor is "an identifier of the session's
    kind" enough (phase-2 security review): a lead known by TWO phones — a second
    number, or one joined to the record by a typed claim — had the reply texted to
    the number the record was keyed on, not the one in the conversation, and stamped
    delivered. The reply goes to the person the conversation is WITH:

      * sms / voice / email → the raw sender the transport carried, persisted on the
        session at ingress (``transport_sender``, migration 0074); for a session from
        before that column, the call record's caller (voice) or the lead identifier
        whose participant key IS the session's (``conversation_sender``). Nothing
        matches → None (``no_recipient_for_channel``), never another identifier of
        the same kind. A voice reply bridges to a text to that caller.
      * Meta → the platform-scoped sender id persisted for the session at ingress
        (item 7). A session from before that column can use the record's own key only
        when it IS a platform-scoped id — never an email or phone from another channel.

    The widget has no transport at all and is reported as ``widget_poll_only`` by the
    sender; nothing is picked for it here."""
    from app.runtime.conversation_sender import conversation_sender  # noqa: PLC0415
    from app.runtime.lead_capture import identifier_kind  # noqa: PLC0415

    if sess.channel in outbound.POLL_ONLY_CHANNELS:
        return None
    lead = await _session_lead(s, admin_id=admin_id, sess=sess)
    if sess.channel in outbound.META_CHANNELS:
        if sess.meta_sender_id:
            return sess.meta_sender_id
        own = lead.contact_identifier if lead is not None else None
        if own and identifier_kind(own) == "transport":
            return own.strip()
        return None
    return await conversation_sender(s, admin_id=admin_id, sess=sess, lead=lead)


async def _bridge_voice_reply_to_sms(
    s: AsyncSession, *, admin_id: uuid.UUID, sess: SessionModel, to: str | None, body: str
) -> outbound.OutboundResult:
    from app.channels.phone_lifecycle import sms_number_status  # noqa: PLC0415
    from app.models.instance import Instance, channel_enabled  # noqa: PLC0415

    inst = (await s.execute(select(Instance).where(Instance.id == sess.instance_id))).scalar_one()
    # A text that starts from the business (no inbound text first) is the case A2P
    # registration governs: the bridge needs the channel ON and the number ATTESTED
    # (`connected`), not merely operable-and-pending.
    attested = (
        await sms_number_status(s, admin_id=admin_id, instance_id=sess.instance_id)
    ) == "connected"
    if not channel_enabled(inst, "sms") or not attested:
        return outbound.OutboundResult(
            channel="voice", delivered=False, detail="voice_reply_requires_sms"
        )
    result = await outbound.send_outbound_reply(
        s, admin_id=admin_id, instance_id=sess.instance_id, channel="sms", to=to, body=body
    )
    if result.delivered:
        return outbound.OutboundResult(
            channel="voice",
            delivered=True,
            provider_message_id=result.provider_message_id,
            detail="voice_bridged_to_sms",
        )
    if result.detail == "sms_sender_not_operable":
        return outbound.OutboundResult(
            channel="voice", delivered=False, detail="voice_reply_requires_sms"
        )
    return outbound.OutboundResult(channel="voice", delivered=False, detail=result.detail)


# The adapter can return a provider-specific failure string (e.g. "twilio_error:...").
# The wire vocabulary is a closed set, so anything unrecognized collapses to send_failed
# — the admin learns it did not go out; the provider detail stays in the audit log.
_WIRE_DELIVERY_DETAILS = frozenset(
    {
        "widget_poll_only",
        "no_recipient",
        "channel_not_provisioned",
        "unsupported_channel",
        # Voice takeover replies (audit F150): went out as a TEXT to the caller, or
        # could not because SMS is off / the number is not connected+attested.
        "voice_bridged_to_sms",
        "voice_reply_requires_sms",
        # Health-gated senders (F070).
        "sms_sender_not_operable",
        "email_sender_not_connected",
        "mailbox_reconnect_needed",
        # Round 7 WP-3b item 1: no identifier of this channel's kind on the lead.
        "no_recipient_for_channel",
        # Round 7 WP-3b item 7: the grant owning the Meta surface cannot send.
        "meta_reconnect_needed",
    }
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
    """Admin live takeover → human_controlled (§3.4.12). Stops the loop for the session.

    An ENDED session is refused with 409 ``session_ended`` (round 7 WP-3b, item 4):
    the customer's transport is no longer waiting on it, so switching it to
    human_controlled would show a person holding a conversation nobody can answer
    into. Nothing is changed, counted or audited on the refusal."""
    async with tenant_session(subject.admin_id) as s:
        sess = await _require_session(s, admin_id=subject.admin_id, session_id=session_id)
        try:
            sess = await handoff.take_over(
                s,
                admin_id=subject.admin_id,
                instance_id=sess.instance_id,
                session_id=session_id,
                actor_user_id=subject.user_id,
                luciel_initiated=False,
            )
        except handoff.SessionEnded:
            raise _session_ended() from None
        # Becoming human-controlled is itself the fire-once budget trigger (§3.4.1b),
        # so the flag is read back AFTER the takeover, not carried in from before it.
        counted = await _counted_for_billing(s, admin_id=subject.admin_id, session_id=session_id)
        return _summary(sess, counted=counted)


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
        counted = await _counted_for_billing(s, admin_id=subject.admin_id, session_id=session_id)
        return _summary(sess, counted=counted)


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
        elif is_retrieval_unavailable(source_chunks_raw):
            # 2026-09-06 E2E walk: the embedding provider failed, so retrieval never
            # ran — the marker is NOT an empty result and must not read as one.
            attribution_status = "retrieval_unavailable"
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
            # Round 7 WP-4c #8: read from the row (migration 0070). This was a
            # constant False — the owner flagged an answer, reloaded, and saw it
            # unflagged.
            flagged_by_admin=msg.flagged_by_admin_at is not None,
        )


@router.post("/conversations/{session_id}/messages/{message_id}/flag", status_code=204)
async def flag_answer(
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    response: Response,
    subject: Subject = Depends(current_admin),
) -> Response:
    """Admin flags a Luciel answer for review (§3.4.13).

    Persisted on the row (``flagged_by_admin_at``, migration 0070 — round 7 WP-4c
    #8) AND audited. The flag was audit-only before, so the evidence read could not
    show it. Idempotent: flagging twice keeps the first timestamp."""
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
        if msg.flagged_by_admin_at is None:
            msg.flagged_by_admin_at = datetime.now(UTC)
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
    limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[EscalationEventOut]:
    """Escalation events for the tenant, newest first, paged (§3.5.2)."""
    limit, offset = _page(limit, offset)
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(EscalationEvent)
                    .where(EscalationEvent.admin_id == subject.admin_id)
                    .order_by(EscalationEvent.fired_at.desc(), EscalationEvent.escalation_id.desc())
                    .offset(offset)
                    .limit(limit)
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
                reasons=[str(x) for x in (r.reasons or [])],
            )
            for r in rows
        ]


# --- leads -------------------------------------------------------------------- #
@dataclass(frozen=True)
class _CrmPosture:
    """The tenant's CURRENT ability to push a lead (round 7 WP-3c item 5).

    Read through the broker's own gates (authorization table + connection status),
    so the reason the dashboard shows for a never-pushed lead is exactly the reason
    the next push would be refused for — never a parallel inference."""

    provider: str | None
    connection_status: str | None
    tool_on: bool

    @property
    def can_push(self) -> bool:
        from app.connections import gate  # noqa: PLC0415

        return self.tool_on and self.connection_status == gate.CONNECTED

    def never_attempted_reason(self, lead: Lead) -> str:
        """Why THIS lead has no CRM record yet (``schemas_api.leads.NeverAttemptedReason``)."""
        from app.connections import gate  # noqa: PLC0415
        from app.runtime.lead_capture import crm_lead_key, crm_phone_key  # noqa: PLC0415

        if self.connection_status != gate.CONNECTED:
            return "dormant" if self.connection_status == "dormant" else "crm_not_connected"
        if not self.tool_on:
            return "tool_off"
        has_key = crm_lead_key(lead) is not None or (
            self.provider == "custom_webhook" and crm_phone_key(lead) is not None
        )
        if not has_key:
            return "no_crm_key"
        return "not_yet_pushed"


async def _crm_posture(
    s: AsyncSession, *, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> _CrmPosture:
    from app.models.connection import InstanceConnection  # noqa: PLC0415
    from app.runtime.crm_push import PUSH_TOOL_ID  # noqa: PLC0415
    from app.tools.broker import ToolBroker  # noqa: PLC0415

    broker = ToolBroker(s)
    tool_on = await broker._authorized(admin_id, instance_id, PUSH_TOOL_ID)  # noqa: SLF001
    status = await broker._connection_status(admin_id, instance_id, "crm")  # noqa: SLF001
    provider = (
        await s.execute(
            select(InstanceConnection.provider).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "crm",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    return _CrmPosture(
        provider=str(provider) if provider else None,
        connection_status=str(status) if status else None,
        tool_on=bool(tool_on),
    )


def _lead_out(r: Lead, posture: _CrmPosture | None = None) -> LeadOut:
    """The ONE lead wire builder (item 9: every lead response goes through it).

    A row nothing ever pushed is an explicit ``never_attempted`` whose detail is the
    reason under the tenant's CURRENT posture; without a posture in hand (a caller
    outside a tenant session) the reason honestly reads ``crm_not_connected`` rather
    than a fabricated ``not_yet_pushed``."""
    if r.crm_push_status in ("synced", "failed"):
        crm_status, crm_detail = r.crm_push_status, r.crm_push_detail
    else:
        crm_status = "never_attempted"
        crm_detail = posture.never_attempted_reason(r) if posture else "crm_not_connected"
    return LeadOut(
        lead_id=r.id,
        name=r.name,
        contact_identifier=r.contact_identifier,
        email=r.email,
        phone=r.phone,
        intent=r.intent,
        state=r.state.value,
        outcome=r.outcome,
        last_activity_at=r.last_activity_at,
        created_at=r.created_at,
        crm_status=crm_status,
        crm_detail=crm_detail,
        crm_pushed_at=r.crm_pushed_at,
    )


async def _lead_out_in(s: AsyncSession, admin_id: uuid.UUID, lead: Lead) -> LeadOut:
    posture = await _crm_posture(s, admin_id=admin_id, instance_id=lead.instance_id)
    return _lead_out(lead, posture)


@router.get("/leads", response_model=list[LeadOut])
async def list_leads(
    limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[LeadOut]:
    """Lead list (§3.4.9). Newest activity first, paged. Archived rows are served
    too — the dashboard keeps them out of the active view, not out of existence."""
    limit, offset = _page(limit, offset)
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(Lead)
                    .where(Lead.admin_id == subject.admin_id)
                    .order_by(Lead.last_activity_at.desc(), Lead.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        # One posture read per instance on the page (one Luciel per account, so in
        # practice one), never one per row.
        postures: dict[uuid.UUID, _CrmPosture] = {}
        out: list[LeadOut] = []
        for r in rows:
            if r.instance_id not in postures:
                postures[r.instance_id] = await _crm_posture(
                    s, admin_id=subject.admin_id, instance_id=r.instance_id
                )
            out.append(_lead_out(r, postures[r.instance_id]))
        return out


#: The most leads one backfill call pushes (item 5): bounded so a tenant with years
#: of leads cannot hold a request open against their CRM's rate limits; the response
#: says how many remain and the dashboard calls again.
CRM_BACKFILL_BATCH = 100
_CRM_BACKFILL_SCAN = 1000


@router.post("/leads/crm-backfill", response_model=CrmBackfillResult)
async def backfill_crm(subject: Subject = Depends(current_admin)) -> CrmBackfillResult:
    """Push every never-attempted, non-archived lead to the CRM (round 7 WP-3c item 5).

    Owner ruling 2026-09-14: a lead captured before a CRM was connected is an explicit
    "never sent" state, and the dashboard offers a ONE-TIME backfill when a CRM
    connects. Idempotent by construction — a pushed lead becomes ``synced`` or
    ``failed`` and drops out of the candidate set, so a second call finds only what
    the first could not reach — bounded to :data:`CRM_BACKFILL_BATCH` leads per call,
    and every push goes through the standard broker path (authorization, connection
    health, the key-ownership guard) exactly like a capture push. 409 when the CRM
    cannot be pushed to at all, so the owner is told what to fix instead of watching
    a button do nothing."""
    from app.runtime.crm_push import push_captured_lead  # noqa: PLC0415
    from app.runtime.lead_capture import (  # noqa: PLC0415
        CaptureResult,
        crm_lead_key,
        crm_phone_key,
    )
    from app.tools.registry import make_tool_resolver  # noqa: PLC0415

    async with tenant_session(subject.admin_id) as s:
        instance_id = (
            await s.execute(select(Instance.id).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if instance_id is None:
            raise not_found("No Luciel.")
        posture = await _crm_posture(s, admin_id=subject.admin_id, instance_id=instance_id)
        if not posture.can_push:
            if posture.connection_status == "dormant":
                raise conflict("Your CRM connection is dormant; add a payment method first.")
            if posture.connection_status != "connected":
                raise conflict("No CRM is connected, so there is nothing to backfill into.")
            raise conflict("The Push to CRM tool is switched off.")
        candidates = (
            (
                await s.execute(
                    select(Lead)
                    .where(
                        Lead.admin_id == subject.admin_id,
                        Lead.instance_id == instance_id,
                        Lead.state == LeadState.active,
                        Lead.crm_push_status.is_(None),
                    )
                    .order_by(Lead.created_at.asc(), Lead.id.asc())
                    .limit(_CRM_BACKFILL_SCAN)
                )
            )
            .scalars()
            .all()
        )

        def _keyed(lead: Lead) -> bool:
            return crm_lead_key(lead) is not None or (
                posture.provider == "custom_webhook" and crm_phone_key(lead) is not None
            )

        keyed = [lead for lead in candidates if _keyed(lead)]
        skipped_no_key = len(candidates) - len(keyed)
        batch, rest = keyed[:CRM_BACKFILL_BATCH], keyed[CRM_BACKFILL_BATCH:]
        attempted = synced = failed = 0
        refused_reason: str | None = None
        resolver = make_tool_resolver()
        for lead in batch:
            receipt = await push_captured_lead(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                captured=CaptureResult(
                    lead_id=lead.id, changed=_current_lead_facts(lead), lead_key=crm_lead_key(lead)
                ),
                lead_key=crm_lead_key(lead),
                snapshot_tool_ids=None,
                tool_resolver=resolver,
                session_id=None,
            )
            if receipt is None:
                skipped_no_key += 1
                continue
            if receipt.refusal_reason is not None:
                # The broker refused for the TENANT (tool off / not ready): nothing
                # was attempted for this lead and nothing will be for the rest.
                refused_reason = receipt.refusal_reason
                rest = [lead, *batch[batch.index(lead) + 1 :], *rest]
                break
            attempted += 1
            if receipt.outcome == "ok":
                synced += 1
            else:
                failed += 1
        remaining = len(rest)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            event_type="crm_backfill_run",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={
                "provider": posture.provider,
                "attempted": attempted,
                "synced": synced,
                "failed": failed,
                "skipped_no_key": skipped_no_key,
                "remaining": remaining,
                "refused_reason": refused_reason,
            },
        )
        await s.flush()
        return CrmBackfillResult(
            attempted=attempted,
            synced=synced,
            failed=failed,
            skipped_no_key=skipped_no_key,
            remaining=remaining,
            refused_reason=refused_reason,
        )


def _current_lead_facts(lead: Lead) -> dict[str, str]:
    """The record's Luciel-maintained facts, as a push payload (the retry path)."""
    facts: dict[str, str] = {}
    if lead.name:
        facts["name"] = lead.name
    if lead.phone:
        facts["phone"] = lead.phone
    elif lead.contact_identifier and "@" not in lead.contact_identifier:
        facts["phone"] = lead.contact_identifier
    if lead.intent:
        facts["intent"] = lead.intent
    if lead.lead_score is not None:
        facts["lead_score"] = f"{lead.lead_score:g}"
    facts["outcome"] = lead.outcome
    return facts


@router.post("/leads/{lead_id}/crm-retry", response_model=LeadOut)
async def retry_crm_push(lead_id: uuid.UUID, subject: Subject = Depends(current_admin)) -> LeadOut:
    """Push the lead's current facts to the CRM again (2026-09-05 audit F134).

    Same broker gates as a capture push (tool off / not connected / dormant → a
    refused, traced no-op). 409 when the push could not even be attempted, so
    the owner is told why instead of watching a button do nothing."""
    from app.runtime.crm_push import push_captured_lead  # noqa: PLC0415
    from app.runtime.lead_capture import CaptureResult, crm_lead_key  # noqa: PLC0415
    from app.tools.registry import make_tool_resolver  # noqa: PLC0415

    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        outcome = await push_captured_lead(
            s,
            admin_id=subject.admin_id,
            instance_id=lead.instance_id,
            captured=CaptureResult(
                lead_id=lead.id, changed=_current_lead_facts(lead), lead_key=crm_lead_key(lead)
            ),
            lead_key=crm_lead_key(lead),
            snapshot_tool_ids=None,
            tool_resolver=make_tool_resolver(),
            session_id=None,
        )
        if outcome is None:
            raise conflict(
                "This lead has no identifier your CRM accepts (an email, or a phone "
                "number for a custom webhook), or no CRM is connected."
            )
        if outcome.refusal_reason is not None:
            # Refused before reaching the CRM (tool off, connection not ready):
            # nothing was attempted, so say what to fix instead of a silent 200.
            raise conflict(f"The push was not attempted: {outcome.refusal_reason}.")
        await s.flush()
        return await _lead_out_in(s, subject.admin_id, lead)


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
        # Item 9: the hand-built LeadOut here dropped phone + the CRM fields, so the
        # row the dashboard re-rendered after Archive read "no phone, no CRM state".
        return await _lead_out_in(s, subject.admin_id, lead)


@router.post("/leads/{lead_id}/unarchive", response_model=LeadOut)
async def unarchive_lead(lead_id: uuid.UUID, subject: Subject = Depends(current_admin)) -> LeadOut:
    """Archived → active by the owner (round 7 WP-3c item 6; the reverse of archive).

    Idempotent: an active lead is returned as-is with no audit row. A reactivated
    lead's activity clock restarts here (the same §3.4.10a rule as archive)."""
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        if lead.state == LeadState.archived:
            lead.state = LeadState.active
            lead.last_activity_at = datetime.now(UTC)
            await audit.emit(
                s,
                admin_id=subject.admin_id,
                instance_id=lead.instance_id,
                event_type="lead_reactivated",
                actor_type=ActorType.admin_user,
                actor_user_id=subject.user_id,
                payload={
                    "resolved_lead_id_hash": audit.hash_lead_id(lead_id),
                    "trigger": "owner_unarchive",
                },
            )
            await s.flush()
        return await _lead_out_in(s, subject.admin_id, lead)


@router.patch("/leads/{lead_id}", response_model=LeadOut)
async def edit_lead(
    lead_id: uuid.UUID,
    body: LeadPatchRequest,
    subject: Subject = Depends(current_admin),
) -> LeadOut:
    """Owner correction of a lead's own facts — name / email / phone (item 6).

    A field the request omits is left alone; an explicit null clears it. Email and
    phone are normalised the way capture normalises them (case-folded / E.164) and a
    number that cannot honestly be E.164 is refused rather than guessed. A value
    ANOTHER lead already owns is a 409, never a silent merge — unification is a
    runtime judgment made on the customer's own words, not an owner keystroke. The
    identifier the edit replaced is kept as a ``superseded`` identifier row (the
    person is still findable by it in the audit of who they were, and it can never
    again resolve a conversation or receive a delivery); the audit row carries the
    changed field NAMES only. A changed email/phone is forwarded to the CRM as an
    update exactly the way a changed phone already is on capture."""
    from app.runtime import lead_capture  # noqa: PLC0415
    from app.runtime.crm_push import push_captured_lead  # noqa: PLC0415
    from app.runtime.lead_capture import CaptureResult, crm_lead_key  # noqa: PLC0415
    from app.tools.registry import make_tool_resolver  # noqa: PLC0415

    sent = body.model_fields_set
    async with tenant_session(subject.admin_id) as s:
        lead = await _require_lead(s, admin_id=subject.admin_id, lead_id=lead_id)
        changed_fields: list[str] = []
        crm_facts: dict[str, str] = {}
        key_moved = False

        if "name" in sent:
            new_name = (body.name or "").strip() or None
            if new_name != lead.name:
                lead.name = new_name
                changed_fields.append("name")
                if new_name:
                    crm_facts["name"] = new_name

        if "email" in sent:
            raw = (body.email or "").strip()
            new_email = lead_capture.normalize_identifier(raw) if raw else None
            if new_email is not None and not lead_capture.is_email_address(new_email):
                raise validation_error("That is not a valid email address.")
            if new_email != lead.email:
                if new_email is not None:
                    holder = await lead_capture.find_lead_by_identifier(
                        s,
                        admin_id=subject.admin_id,
                        instance_id=lead.instance_id,
                        value=new_email,
                        exclude_id=lead.id,
                    )
                    if holder is not None:
                        raise conflict("That email address already belongs to another lead.")
                await lead_capture.supersede_identifier(
                    s, lead, kind="email", old=lead.email, new=new_email
                )
                lead.email = new_email
                changed_fields.append("email")
                key_moved = True

        if "phone" in sent:
            raw = (body.phone or "").strip()
            new_phone = lead_capture.normalize_phone(raw) if raw else None
            if raw and new_phone is None:
                raise validation_error(
                    "That phone number could not be read as an international (E.164) number."
                )
            if new_phone != lead.phone:
                if new_phone is not None:
                    holder = await lead_capture.find_lead_by_identifier(
                        s,
                        admin_id=subject.admin_id,
                        instance_id=lead.instance_id,
                        value=new_phone,
                        exclude_id=lead.id,
                    )
                    if holder is not None:
                        raise conflict("That phone number already belongs to another lead.")
                await lead_capture.supersede_identifier(
                    s, lead, kind="phone", old=lead.phone, new=new_phone
                )
                lead.phone = new_phone
                changed_fields.append("phone")
                if new_phone:
                    crm_facts["phone"] = new_phone

        if not changed_fields:
            return await _lead_out_in(s, subject.admin_id, lead)

        # An owner edit is the owner working the lead (§3.4.10a, audit F096).
        lead.last_activity_at = datetime.now(UTC)
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=lead.instance_id,
            event_type="lead_edited",
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            payload={
                "resolved_lead_id_hash": audit.hash_lead_id(lead_id),
                # Field NAMES only (§3.4.11) — never the old or new values.
                "changed_fields": changed_fields,
            },
        )
        # Forward the change to the CRM through the standard broker gates. A moved
        # email is the CRM KEY moving: the record's current facts ride so the push
        # is non-empty and the backend can tell the CRM about the new address (as a
        # note — the CRM's own Email property is never rewritten, item 2). A push
        # failure never fails the edit; the lead row is the source of truth.
        try:
            await push_captured_lead(
                s,
                admin_id=subject.admin_id,
                instance_id=lead.instance_id,
                captured=CaptureResult(
                    lead_id=lead.id,
                    changed=_current_lead_facts(lead) if key_moved else crm_facts,
                    lead_key=crm_lead_key(lead),
                ),
                lead_key=crm_lead_key(lead),
                snapshot_tool_ids=None,
                tool_resolver=make_tool_resolver(),
                session_id=None,
            )
        except Exception:  # noqa: BLE001 — the edit must never fail on a push
            logger.exception("lead_edit_crm_push_failed")
        return await _lead_out_in(s, subject.admin_id, lead)


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
        return await _lead_out_in(s, subject.admin_id, lead)


def _summary(sess: SessionModel, *, counted: bool) -> ConversationSummaryOut:
    return ConversationSummaryOut(
        session_id=sess.id,
        lead_id=sess.resolved_lead_id,
        channel=sess.channel,
        mode=sess.mode,
        started_at=sess.started_at,
        last_message_at=sess.last_message_at,
        summary=sess.summary,
        ended_at=sess.ended_at,
        counted_for_billing=counted,
    )
