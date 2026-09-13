"""Agentic loop, budget gate, escalation wiring (DOCTRINE ANCHOR — Architecture §3.4.1).

The loop, per inbound message:

  Half A — intake:
    RECEIVE
      → CONVERSATION BUDGET GATE (§3.4.1b): at-cap with no payment method → graceful
        single-turn reply WITHOUT an LLM call + cannot_answer/budget_exhausted
        escalation; the counter does NOT increment (no model call, no handoff).
      → MODERATION (§3.4.15): harmful inbound escalates to a human, no harmful output.
      → ESCALATION GATE 1 — INTAKE (§3.4.5): explicit_human_request or
        strong_negative_sentiment → acknowledge, hand off, skip the loop.
      → RETRIEVE (§3.2): knowledge retrieval; all retrieved content is wrapped as
        UNTRUSTED data before it ever reaches the model (§3.4.14 Defense).
      → [PLAN → ACT → REFLECT] × up to 5 (§3.4.1 iteration bound — cost control):
        the first model call fires the budget counter once (§3.4.1b). Tool calls
        dispatch via the Arc 12 broker (§3.3.3) using the five-outcome contract.
      → GROUNDING (§3.4.13): score the answer against retrieved chunks / tool result.
      → ESCALATION GATE 2 — OUTCOME (§3.4.5): high_value_lead (checked first — an
        opportunity signal independent of answer quality), else cannot_answer (low
        confidence OR below the 0.50 grounding floor, with no resolving tool —
        harmony fix 2026-08-04 item 3: AND made this gate unsatisfiable in live mode
        because providers always report confidence=1.0).

  Half B — emission:
    RESPOND: channel arbiter picks the outbound channel (§3.4.2).
    FINALIZATION: persist transcript; the summary is produced at session close
      (§3.4.7), not per message — the loop persists turns; summarization fires at
      session end via app/runtime/session_close.py (the hourly session_finalize
      sweep on inactivity timeout, or an explicit end signal such as the widget
      close beacon).

The LLM's instructions are NOT a security boundary (§3.4.14 / §7 #36): tenant
isolation (RLS), tool authorization (broker default-deny), and budget all hold in
code regardless of what the model is convinced to say.

An escalation writes the escalation_events row + audit event and THEN notifies the
admin's configured contact (§3.5.3, app/notifications/escalation_dispatch.py); delivery
is best-effort against the row and never blocks the lead's turn (§3.5.4).

FLAGGED: Vector RETRIEVAL is Arc 11 and graph/hybrid is Arc 16
— the loop calls a retrieval callable injected by the caller so it is testable now
and the production retriever binds behind the same signature.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.employee.context import OperatingContext
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Session, Transcript
from app.models.instance import Instance
from app.notifications.escalation_dispatch import (
    has_configured_escalation_contact,
    notify_escalation_safely,
)
from app.observability.metrics import emit_metric
from app.runtime import budget, channel_switch, handoff, memory
from app.runtime import personality as personality_rules
from app.runtime.channel_arbiter import choose_outbound_channel
from app.runtime.escalation import (
    EscalationDecision,
    Signal,
    evaluate_intake,
    evaluate_outcome,
    is_conversational_turn,
    is_repeated_question,
)
from app.runtime.grounding import faithful_readback_ok, score_grounding
from app.runtime.input_safety import (
    ModerationCategory,
    build_system_prompt,
    detect_injection,
    ensure_ai_identity_disclosure,
    moderate,
    pre_send_check,
    wrap_untrusted,
)
from app.runtime.knowledge_retrieval import RetrieverBundle
from app.runtime.lead_capture import capture_or_update_lead
from app.runtime.llm_router import LLMResponse, LLMRouter, ProviderError
from app.runtime.team_availability import follow_up_phrase
from app.runtime.team_availability import parse as parse_team_availability
from app.tools import contract as tool_contract
from app.tools.contract import Tool, ToolResult
from app.tools.schemas import ACTION_TOOLS, build_tool_defs

MAX_ITERATIONS = 5  # §3.4.1 — internal cost-control constant, identical on every tier.

# §3.3.7 give-up condition (audit round 3, C4): after this many consecutive turns in
# which the broker rejected the model's tool intent as missing_input/ambiguous with
# nothing resolving, Luciel stops re-asking and hands the thread to a human with the
# partial context. Deterministic, in code — never trusted to the model.
ASK_ATTEMPT_BOUND = 2

# Structured tool feedback caps (C4): the redacted result JSON fed back to the model
# is length-capped, and an ambiguous candidate list is truncated, so a huge record or
# candidate set cannot blow out the context window.
TOOL_RESULT_CONTENT_CAP = 2000
AMBIGUOUS_CANDIDATE_CAP = 5

_LOG = logging.getLogger(__name__)

# Conservative success-claim detector (§3.3.4 rule 1 backstop): verbs a reply may not
# use about an ACTION tool when no action returned `ok` this turn.
_SUCCESS_CLAIM = re.compile(
    r"\b(booked|scheduled|rescheduled|cancelled|canceled|confirmed|sent|delivered|"
    r"added to (?:the|your) calendar|updated your)\b",
    re.IGNORECASE,
)

# The same guard for a turn in which NO action tool ran at all (2026-09-05 audit
# F110): first-person claims only, so a knowledge answer that merely contains the
# word "confirmed" ("orders are confirmed by email") is left alone.
_FIRST_PERSON_SUCCESS_CLAIM = re.compile(
    r"\b(i'?ve|i have|we'?ve|we have|i|we|it'?s been|it has been|has been|have been"
    r"|is now|are now)\b"
    r"[^.!?]{0,30}?"
    r"\b(booked|scheduled|rescheduled|cancelled|canceled|sent|delivered"
    r"|added to (?:the|your) calendar|updated your)\b",
    re.IGNORECASE,
)


def is_clarifying_question(text: str) -> bool:
    """A single short question with no assertion in it — the one ungrounded draft that
    is honest to send, because it claims nothing (F100)."""
    body = (text or "").strip()
    if not body.endswith("?") or len(body) > 160:
        return False
    return sum(body.count(ch) for ch in ".!?") == 1


#: Tools whose ``ok`` result the reply READS BACK — a booking, a free/busy answer, a
#: record lookup — and that therefore ground it (2026-09-05 audit F119). A CRM push or
#: a queued callback succeeding says nothing about whether the words are true.
ANSWER_BEARING_TOOLS = frozenset(
    {
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "lookup_record",
    }
)

#: Compassion first, honesty second, a person third (2026-09-05 audit F116). No
#: contact ask is appended: the person in distress is not asked for a phone number.
SELF_HARM_REPLY = (
    "I'm really sorry you're going through this. If you're in immediate danger, please "
    "contact your local emergency number right now — in Canada and the US you can call "
    "or text 988 to reach a crisis line, any time. I've also let the team here know so a "
    "person can follow up with you."
)

#: Honest offers when the arbiter (§3.4.2) would rather answer on another channel
#: (2026-09-05 audit F060): nothing is sent on that channel without the lead's say-so.
_CHANNEL_OFFER = {
    "email": " If you'd like the full details by email, just share your email address.",
    "sms": " I can also text you a short version if you share your mobile number.",
}

# Honest replacement when the model claimed an action that did not happen (C4). The
# copy follows the escalation-honesty doctrine: passed to the team, never "connecting".
_UNFOUNDED_CLAIM_REPLACEMENT = (
    "I wasn't able to complete that just now — I've flagged it for the team, and "
    "someone will follow up with you shortly."
)

# Retriever: (query) -> list of retrieved chunk texts. Vector impl lands Arc 11,
# graph/hybrid Arc 16 (§3.2). Injected so the loop is testable now.
Retriever = Callable[[str], Awaitable[list[str]]]

# ChunkAttributor: (query) -> real retrieved-chunk metadata (harmony wave 2,
# item 2a). ADDITIVE seam alongside Retriever -- see
# app/runtime/knowledge_retrieval.py::make_chunk_attributor for why this doesn't
# change the Retriever contract itself. None when the caller has no attribution
# source bound (voice/tests using a bare fake retriever); the loop then persists
# source_chunks=None (attribution_unavailable) rather than fabricating a list.
ChunkAttributor = Callable[[str], Awaitable[list[dict[str, object]]]]

# ToolResolver: (tool_id) -> a Tool instance (with its Arc 17 backend bound) or None.
# The catalog tools require injected connection backends (Arc 17), so the loop takes
# a resolver rather than constructing the catalog itself; the broker still enforces
# default-deny authorization + connection gating in code (§3.3.3).
ToolResolver = Callable[[str], Tool | None]

# At-cap graceful reply — no LLM call (§3.4.1b). Mirrors the frozen widget contract
# wording shape (widget-adapters.ts): an acknowledgement that the message was passed
# along, never a silent drop.
AT_CAP_REPLY = (
    "Thanks for reaching out — I've passed your message along and someone will be in touch shortly."
)


@dataclass
class LoopResult:
    reply_text: str
    outbound_channel: str
    counted: bool
    escalation: EscalationDecision | None = None
    grounding_score: float | None = None
    used_fallback: bool = False
    at_cap: bool = False
    handed_off: bool = False
    iterations: int = 0
    tool_outcomes: list[str] = field(default_factory=list)
    # The persisted transcript row of the reply, when one was written (2026-09-06
    # E2E walk): the widget hands this id back as the reply's id so its history
    # poll recognises the same row and never renders the reply twice.
    reply_message_id: uuid.UUID | None = None


TRANSCRIPT_ROLES = frozenset({"lead", "luciel", "human_agent", "system"})


async def persist_turn(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    text: str,
    grounding_score: float | None = None,
    source_chunks: list[dict[str, object]] | None = None,
    at: datetime | None = None,
    deterministic: bool = False,
) -> Transcript:
    """Append one message to the session transcript and return the persisted row.

    Public because the takeover reply path (§3.4.12) writes ``human_agent`` rows through
    the SAME function the loop uses — one transcript writer, so an admin reply and a
    Luciel reply are indistinguishable in ordering/tenancy handling.

    ``grounding_score`` (harmony fix 2026-08-04, item 3) persists the real per-turn
    score computed by score_grounding for ``luciel`` replies, so the dashboard evidence
    endpoint can read the actual value instead of a hardcoded floor constant. Left
    None for non-luciel roles and for the technical-issue ack (no grounding was
    computed — no model call was served).

    ``source_chunks`` (harmony wave 2, item 2a) persists the REAL retriever chunk
    refs (source_id/source_name/text) used for this reply, when the caller has an
    attribution source bound. None (the default) means attribution was never
    captured for this row — that is NOT a claim that no source backed the answer,
    just that we don't know. An empty list means retrieval genuinely ran and found
    nothing. The dashboard evidence endpoint keeps these three states distinct.

    ``at`` (live-caught 2026-08-17): the column's server default is Postgres
    ``now()`` — TRANSACTION timestamp, constant across the whole request — so a
    turn's lead row and reply row landed with an identical ``at`` and the
    reply-time analytics read 0s forever. Callers now stamp the honest moment:
    arrival time for the inbound row, wall-clock persist time for the reply.

    ``deterministic`` (#5c, live-caught 2026-08-23, migration 0051): True for luciel
    rows composed in CODE (greeting, at-cap reply, escalation acks, LLM-outage ack).
    Scoring does not apply to these, and the evidence endpoint serves
    ``scoringStatus: "not_applicable"`` instead of the false temporal claim
    "not_scored_legacy" a fresh greeting row used to carry."""
    if role not in TRANSCRIPT_ROLES:
        raise ValueError(f"unknown transcript role: {role}")
    row = Transcript(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        role=role,
        text=text,
        grounding_score=grounding_score,
        source_chunks=source_chunks,
        deterministic=deterministic,
    )
    if at is not None:
        row.at = at
    session.add(row)
    await session.flush()
    return row


async def _session_already_escalated(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID, signal: str
) -> bool:
    """True when this session already carries an escalation row for this signal."""
    row = (
        await session.execute(
            select(EscalationEvent.escalation_id)
            .where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.session_id == session_id,
                EscalationEvent.signal == signal,
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def record_platform_escalation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    signal: str,
    detail: str | None = None,
    resolved_lead_id: uuid.UUID | None = None,
) -> bool:
    """Record + notify a PLATFORM-originated escalation for a session, once per signal.

    The public door for edges outside the loop (an undeliverable reply, 2026-09-05
    audit F077): same row, same audit event, same notification path as the judgment
    gates, and the same once-per-session rule. ``detail`` (the delivery failure
    reason) rides the audit trail, never the customer-facing reply. Returns whether a
    new row was written."""
    if await _session_already_escalated(
        session, admin_id=admin_id, session_id=session_id, signal=signal
    ):
        return False
    if resolved_lead_id is None:
        resolved_lead_id = (
            await session.execute(
                select(Session.resolved_lead_id).where(
                    Session.admin_id == admin_id, Session.id == session_id
                )
            )
        ).scalar_one_or_none()
    if detail:
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="escalation_delivery_failed",
            actor_type=ActorType.platform,
            payload={
                "signal": signal,
                "gate": "outcome",
                "session_id": str(session_id),
                "failure_reason": detail,
                "context": "customer_reply_undeliverable",
            },
        )
    await _record_escalation(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate="outcome",
        score=None,
    )
    return True


async def _record_escalation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
    reasons: list[str] | None = None,
) -> bool:
    """Write the escalation_events row + the audit event, then notify (§3.5.2 / §5.2).

    The row is written and flushed FIRST: it is the authoritative record and delivery is
    best-effort against it (§3.5.2). Notification never blocks or breaks the lead's turn
    (§3.5.4).

    ONCE per (session, signal, gate): the insert itself is the arbiter (unique index
    ``uq_escalation_once``, 2026-09-05 audit F053/F061) — a pre-check-then-insert let
    two turns racing on the same session page the owner twice. Returns whether this
    call wrote the row; a loser writes nothing and notifies nobody."""
    inserted = await session.execute(
        pg_insert(EscalationEvent)
        .values(
            escalation_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score_or_confidence=score,
            reasons=reasons or None,
            delivery_status="pending",
        )
        .on_conflict_do_nothing(
            index_elements=["admin_id", "session_id", "signal", "gate"],
            index_where=EscalationEvent.session_id.isnot(None),
        )
    )
    if inserted.rowcount == 0:  # type: ignore[attr-defined]  # a racing turn already wrote it
        return False
    emit_metric("EscalationFired", dimensions={"Signal": signal})
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type="escalation_fired",
        actor_type=ActorType.luciel_runtime,
        # No raw lead PII in the payload (§3.4.11) — signal + session id only.
        payload={"signal": signal, "gate": gate, "session_id": str(session_id)},
    )
    await session.flush()
    await notify_escalation_safely(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate=gate,
        score=score,
    )
    return True


@dataclass
class Orchestrator:
    """Wires the cognition modules into the agentic loop (§3.4.1).

    Construct per-request with the tenant-scoped session, the LLM router, and a
    retriever. enabled_channels comes from the instance config (§3.4.2 arbiter)."""

    session: AsyncSession
    router: LLMRouter
    retriever: Retriever
    admin_id: uuid.UUID
    instance_id: uuid.UUID
    enabled_channels: set[str]
    # Harmony wave 2, item 2a: optional real chunk-attribution seam bound alongside
    # `retriever`. None means this construction site has no attribution source
    # (e.g. voice, or a test's bare fake retriever) -- source_chunks is then
    # persisted as None (attribution_unavailable), never fabricated.
    chunk_attributor: ChunkAttributor | None = None
    # Single-pass retrieval (cost, 2026-09-05 audit): when bound, ONE vector query
    # yields both the prompt snippets and the attribution; ``retriever`` and
    # ``chunk_attributor`` are then not called. Unbound → the two-seam path above.
    retriever_bundle: RetrieverBundle | None = None
    # Admin-provided 280-char business-context (Vision §3.5). Composed into the system
    # prompt UNDER the platform-invariant stanzas via Defense 6 (§3.4.14) so it can
    # tune tone/facts but cannot override AI-identity disclosure, grounding, the
    # escalation contract, or moderation.
    business_context: str | None = None
    # Business + assistant display names for the AI-identity disclosure backstop
    # (§3.4.16). Used to compose the guaranteed first-message disclosure on text
    # channels when the model's own reply omits it.
    business_name: str = "this business"
    assistant_name: str = "the assistant"
    # The tenant's provisioned email address (§3.1.6a / Decision #49). When set, Luciel
    # is told its own mailbox at prompt-composition time so it self-identifies by it and
    # recognizes mail to it. Passed by the caller when the channel is email.
    luciel_email_address: str | None = None
    # The admin's personality pillar (preset or custom axes, Vision §3.5). Composed
    # into a platform-authored stanza under the invariants so the selected preset
    # actually shapes how Luciel talks. None = the default preset.
    personality: dict[str, object] | None = None
    system_prompt: str = field(default="", init=False)
    # REQUIRED and keyword-only, deliberately undefaulted (P2-5). A default of False
    # means a call site that forgets it caps a paying PAYG tenant at the free 50 and
    # says nothing — the customer is refused service, and it looks identical to being
    # genuinely out of allowance. Resolve it from ``metering.has_live_card``.
    has_payment_method: bool = field(kw_only=True)
    # Resolves a proposed tool_id to a Tool with its Arc 17 backend bound; None when
    # tools are not wired (the default in Arc 14). A None resolution yields a `failed`
    # outcome so the loop reasons about it uniformly (§3.4.1).
    tool_resolver: ToolResolver | None = None
    # (channel, source id) the inbound edge rate-limits this turn under, so a detected
    # prompt injection counts toward that source's auto-block (2026-09-05 audit F086).
    # None where no source is known (tests, internal callers) — then it only audits.
    abuse_source: tuple[str, str] | None = None
    # §3.8.7 rule E (Decision #39): the CAPABILITY set of tools enabled when THIS
    # conversation began, taken from the session's config_snapshot. A tool enabled
    # AFTER open is an additive change that must apply to the NEXT conversation, not
    # this one — so a proposed tool_id outside this set is treated as unavailable even
    # if the live instance now authorizes it. None = no snapshot (older sessions) →
    # fall back to the broker's live gates alone. This is CAPABILITY=snapshot; the
    # broker's connection/authorization gates remain LIVE (usability=live, safety wins).
    snapshot_tool_ids: set[str] | None = None
    # The employee's operating context (round 6 WP-F): business short name, live
    # channels, how the team is notified, whether a person is reachable. Platform-
    # resolved by the entry point; None keeps the prompt byte-identical.
    operating_context: OperatingContext | None = None

    def __post_init__(self) -> None:
        # Defense 6 (§3.4.14): compose the system prompt with the platform-invariant
        # stanzas at higher precedence than the admin business-context field, so admin
        # text can never suppress AI-identity disclosure, grounding, escalation, or
        # moderation. This is the in-code enforcement; it does not depend on the model.
        self.system_prompt = build_system_prompt(
            self.business_context,
            self.luciel_email_address,
            self.personality,
            # The tenant's configured name — the model must self-identify by it,
            # not by the platform's product name (live-caught 2026-08-17: greeting
            # said "Aurora", the model then said "I'm Luciel").
            assistant_name=self.assistant_name,
            operating_context=self.operating_context,
        )

    async def run_turn(
        self,
        *,
        session_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None,
        channel: str,
        mode: str,
        inbound_text: str,
        transport_identifier: str | None = None,
        received_at: datetime | None = None,
    ) -> LoopResult:
        """Run one inbound message through the loop. `mode` is the session's current
        conversation mode (ai | human_controlled). `transport_identifier` is the
        channel's stable sender key (phone/email/meta-id) used for lead capture +
        identity resolution (§3.4.4/§3.4.9); None for the weak widget channel.
        `received_at` is the inbound message's TRUE arrival time (adapter
        received_at / request entry) — stamped on the lead transcript row so the
        reply-time analytics measure real latency, not the shared transaction
        timestamp (live-caught 2026-08-17: every widget turn read 0s)."""
        admin_id, instance_id = self.admin_id, self.instance_id

        # Human-controlled sessions STOP the loop (§3.4.12): persist the inbound for
        # the dashboard live feed, make no LLM call.
        if not handoff.loop_should_run(mode):
            await persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="lead",
                text=inbound_text,
                at=received_at,
            )
            return LoopResult(
                reply_text="",
                outbound_channel=channel,
                counted=False,
                handed_off=True,
            )

        await persist_turn(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role="lead",
            text=inbound_text,
            at=received_at,
        )

        # --- LEAD CAPTURE COGNITION (§3.4.4 / §3.4.9) — always on ------------ #
        # Write/maintain the platform-owned Lead record and promote a weak widget
        # token to a real lead once a strong identifier is volunteered. The returned
        # id (if any) is threaded through escalation payloads + finalization so the
        # dashboard leads view and escalation notifications are actually populated.
        # Runs BEFORE the budget gate (C5 — audit round 3): capture is deterministic
        # cognition with no LLM call, and the docs promise "leads still captured +
        # escalated" AT CAP — the at-cap escalation row used to carry no lead id
        # because capture only ran after the gate.
        sess_obj = (
            await self.session.execute(
                select(Session).where(Session.admin_id == admin_id, Session.id == session_id)
            )
        ).scalar_one_or_none()
        lead_context: str | None = None
        if sess_obj is not None:
            captured = await capture_or_update_lead(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                sess=sess_obj,
                inbound_text=inbound_text,
                channel=channel,
                transport_identifier=transport_identifier,
            )
            if captured is not None:
                resolved_lead_id = captured.lead_id
                # Round 5C item 6: the record's owner-facing facts ride into the
                # prompt as untrusted DATA (pure facts, no in-band instructions,
                # no contact identifiers — §3.4.9) so Luciel stops re-asking a
                # name it has on file.
                facts = []
                if captured.known_name:
                    facts.append(f"name: {captured.known_name}")
                if captured.known_intent:
                    facts.append(f"interest: {captured.known_intent}")
                if facts:
                    lead_context = "Lead record on file — " + "; ".join(facts)
                # Deterministic CRM push-on-capture (Phase 6.5): a fact that
                # actually changed goes to the tenant's CRM through the SAME
                # broker gates a model-proposed push would face — the Journey's
                # "keeps each lead's record current" no longer depends on the
                # model remembering to call the tool. Runs on every path that
                # captures (including at-cap and moderation turns below), and a
                # refused/failed push never touches the customer reply.
                from app.runtime.crm_push import push_captured_lead  # noqa: PLC0415

                await push_captured_lead(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    captured=captured,
                    lead_key=captured.lead_key,
                    snapshot_tool_ids=self.snapshot_tool_ids,
                    tool_resolver=self.tool_resolver,
                    session_id=session_id,
                )

        # --- CONVERSATION BUDGET GATE (§3.4.1b) — before PLAN ----------------- #
        budget_state = await budget.check_budget(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            has_payment_method=self.has_payment_method,
        )
        if budget_state.at_cap:
            # Graceful single-turn reply WITHOUT an LLM call; escalation row signal
            # budget_exhausted; counter does NOT increment (§3.4.1b).
            emit_metric("BudgetGateHit", dimensions={"Channel": channel})
            await self._emit_budget_exhausted(session_id, resolved_lead_id)
            at_cap_reply = self._with_contact_ask(
                AT_CAP_REPLY,
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
            )
            at_cap_row = await persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=at_cap_reply,
                at=datetime.now(UTC),
                deterministic=True,
            )
            return LoopResult(
                reply_message_id=at_cap_row.id,
                reply_text=at_cap_reply,
                outbound_channel=channel,
                counted=False,
                at_cap=True,
                # The decision names the SAME signal the escalation row records (C4
                # harmony fix — it used to say cannot_answer while the row said
                # budget_exhausted).
                escalation=EscalationDecision(True, Signal.budget_exhausted),
            )

        # --- CHANNEL-SWITCH CONFIRMATION (round 6 WP-H, audit F060) ------------ #
        # The customer asked for the last answer on another channel and was asked
        # where to send it; this turn is their answer. Deterministic — no model call —
        # and nothing goes to an address they did not confirm.
        if sess_obj is not None and sess_obj.pending_outbound:
            settled = await self._settle_pending_outbound(
                sess_obj,
                session_id=session_id,
                channel=channel,
                inbound_text=inbound_text,
                resolved_lead_id=resolved_lead_id,
            )
            if settled is not None:
                return settled

        # --- CONTENT MODERATION (§3.4.15) ------------------------------------ #
        mod = moderate(inbound_text)
        if mod.should_escalate:
            await self._audit_moderation(session_id, mod.category.value)
            if mod.category is ModerationCategory.self_harm:
                # Immediate escalation with crisis copy; no contact ask (F116).
                ack = SELF_HARM_REPLY
            else:
                ack = self._with_contact_ask(
                    await self._handoff_ack(),
                    channel=channel,
                    resolved_lead_id=resolved_lead_id,
                    transport_identifier=transport_identifier,
                )
            return await self._hand_off_with_ack(
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                channel=channel,
                signal=Signal.explicit_human_request,
                gate="intake",
                score=None,
                ack=ack,
            )

        # Monitoring-only injection signal (NOT a security boundary, §3.4.14).
        if detect_injection(inbound_text):
            await self._audit_injection(session_id)

        # --- ESCALATION GATE 1 — INTAKE (§3.4.5) ----------------------------- #
        history = await memory.load_session_history(
            self.session, admin_id=admin_id, session_id=session_id
        )
        trailing = [t.text for t in history if t.role == "lead"]
        intake = evaluate_intake(trailing)
        if intake.fired:
            return await self._hand_off_with_ack(
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                channel=channel,
                signal=intake.signal or Signal.explicit_human_request,
                gate="intake",
                score=intake.score_or_confidence,
                reasons=intake.reasons,
                ack=self._with_contact_ask(
                    await self._handoff_ack(),
                    channel=channel,
                    resolved_lead_id=resolved_lead_id,
                    transport_identifier=transport_identifier,
                ),
            )

        # --- RETRIEVE (§3.2) — wrap as UNTRUSTED before the model (§3.4.14) --- #
        # Harmony wave 2, item 2a: real chunk attribution for persistence, kept
        # separate from `raw_chunks` (which only ever feeds the model/scorer).
        # None when this construction site has no attribution source bound (voice,
        # bare fakes in tests) -- persisted as attribution_unavailable, never
        # fabricated. When bound, an empty list here is a REAL "retrieval found
        # nothing", distinct from None.
        attributed_chunks: list[dict[str, object]] | None = None
        if self.retriever_bundle is not None:
            raw_chunks, attributed_chunks = await self.retriever_bundle(inbound_text)
        else:
            raw_chunks = await self.retriever(inbound_text)
            if self.chunk_attributor is not None:
                attributed_chunks = await self.chunk_attributor(inbound_text)
        wrapped_chunks = [wrap_untrusted(c) for c in raw_chunks]

        # --- CROSS-SESSION MEMORY (§3.4.6) ----------------------------------- #
        # Prior-session SUMMARIES for this lead (never full transcripts, §3.4.8) —
        # bounded N=10/12-month injection (§3.4.10), gated by the §3.4.9 HARD RULE
        # inside load_cross_session_summaries (weak channel / no lead → []). This is
        # what makes "good to hear from you again" real for a returning lead.
        prior_summaries = await memory.load_cross_session_summaries(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            channel=channel,
            resolved_lead_id=resolved_lead_id,
            exclude_session_id=session_id,
        )

        # --- TOOL DEFS for this turn (C4) ------------------------------------ #
        # The model is told about exactly the tools this conversation may dispatch:
        # snapshot capability (§3.8.7 rule E) ∩ live enabled authorizations ∩
        # resolver-known. The broker still enforces every gate in code at dispatch
        # (§3.4.14 Defense 1) — this only determines what the model is TOLD.
        tool_defs = await self._turn_tool_defs()
        system_prompt = (
            build_system_prompt(
                self.business_context,
                self.luciel_email_address,
                self.personality,
                assistant_name=self.assistant_name,
                tool_catalog=tool_defs,
                operating_context=self.operating_context,
            )
            if tool_defs
            else self.system_prompt
        )

        # --- PLAN → ACT → REFLECT × up to 5 (§3.4.1) ------------------------- #
        resp, iterations, used_fallback, llm_failed = await self._reason(
            inbound_text,
            history,
            wrapped_chunks,
            resolved_lead_id=resolved_lead_id,
            prior_summaries=prior_summaries,
            lead_context=lead_context,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            session_id=session_id,
        )

        if used_fallback:
            # The fallback model answered (F132): visible in the owner's audit trail and
            # the data-plane log, never a silent quality drop. Once per turn is the honest
            # grain — an outage that lasts a conversation shows as that many rows.
            _LOG.warning("llm_fallback_used session=%s", session_id)
            emit_metric("LLMFallbackUsed", dimensions={"Channel": channel})
            await audit.emit(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                event_type="llm_fallback_used",
                actor_type=ActorType.luciel_runtime,
                payload={"session_id": str(session_id), "channel": channel},
            )
        if llm_failed:
            # Both providers down → llm_unavailable escalation (§3.4.5 server signal).
            await self._emit_llm_unavailable(session_id, resolved_lead_id)
            ack = self._with_contact_ask(
                "I'm having a brief technical issue — I've flagged this so a person "
                "can follow up with you shortly.",
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
            )
            outage_row = await persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=ack,
                at=datetime.now(UTC),
                deterministic=True,
            )
            # The first model-call attempt is the fire-once trigger even on failure?
            # No: §3.4.1b ties the counter to a model CALL succeeding/serving. A pure
            # provider outage with no served turn does not count. Flagged: revisit
            # with metering in Arc 18.
            return LoopResult(
                reply_message_id=outage_row.id,
                reply_text=ack,
                outbound_channel=channel,
                counted=False,
                # Same-signal harmony as the persisted llm_unavailable row (C4).
                escalation=EscalationDecision(True, Signal.llm_unavailable),
                iterations=iterations,
            )

        # First model call succeeded → fire-once budget counter (§3.4.1b).
        counted = await self._count_served_turn(session_id)

        # --- §3.3.7 ASK-ATTEMPT BOUND (C4) ----------------------------------- #
        # A turn that ended with the broker rejecting the model's tool intent as
        # missing_input/ambiguous and nothing resolving is a STALLED GATHER: the
        # model's reply is (per the T1/T3 stanzas) a question back to the customer.
        # Asking is bounded, not infinite — after ASK_ATTEMPT_BOUND consecutive
        # stalled turns, Luciel stops re-asking and hands off with the context
        # (§3.3.7 rule 4). An `ok` outcome resets the counter: the gather worked.
        any_ok = any(r.outcome is tool_contract.Outcome.ok for _, r in resp.dispatched)
        stalled = not any_ok and any(
            r.outcome in (tool_contract.Outcome.missing_input, tool_contract.Outcome.ambiguous)
            for _, r in resp.dispatched
        )
        # Round 5C item 18: a model that re-asks in PROSE (no tool proposed, so no
        # broker rejection) is the same stall — the bound must count it or
        # "repeated re-asking" (§3.3.7's prohibited failure mode) is unbounded.
        # Deterministic: this reply's last question near-matches the previous
        # luciel reply's last question. Distinct gather questions never match.
        if not stalled and not resp.dispatched:
            previous_luciel = next((t.text for t in reversed(history) if t.role == "luciel"), None)
            stalled = is_repeated_question(resp.text, previous_luciel)
        if sess_obj is not None:
            if stalled:
                sess_obj.input_ask_attempts += 1
                if sess_obj.input_ask_attempts >= ASK_ATTEMPT_BOUND:
                    sess_obj.input_ask_attempts = 0
                    return await self._hand_off_with_ack(
                        session_id=session_id,
                        resolved_lead_id=resolved_lead_id,
                        channel=channel,
                        signal=Signal.cannot_answer,
                        gate="outcome",
                        score=None,
                        ack=self._with_contact_ask(
                            await self._handoff_ack(),
                            channel=channel,
                            resolved_lead_id=resolved_lead_id,
                            transport_identifier=transport_identifier,
                        ),
                    )
            elif any_ok and sess_obj.input_ask_attempts:
                sess_obj.input_ask_attempts = 0

        # --- GROUNDING (§3.4.13) --------------------------------------------- #
        # Only an ANSWER-BEARING tool grounds the words (F119): a CRM push or a queued
        # callback succeeding is not evidence the reply's facts are true.
        tool_succeeded = any(
            str(call.get("tool_id", "")) in ANSWER_BEARING_TOOLS
            and r.outcome is tool_contract.Outcome.ok
            for call, r in resp.dispatched
        )
        grounding = score_grounding(resp.text, raw_chunks, tool_succeeded, question=inbound_text)

        # --- ESCALATION GATE 2 — OUTCOME (§3.4.5) ---------------------------- #
        outcome = evaluate_outcome(
            message=inbound_text,
            plan_confidence=resp.confidence,
            grounding_score=grounding.score,
            # The owner's own hot-lead phrases (F102): the one admin input to the fixed
            # high_value_lead signal, matched over the same recent turns.
            custom_rules_hit=personality_rules.custom_rules_hit(
                self.personality, " ".join([*trailing[-5:], inbound_text])
            )
            or False,
            resolving_tool_succeeded=tool_succeeded,
            # The lead's recent turns (F106): a budget in one message and "this week"
            # in the next is one hot lead, not two lukewarm messages.
            context=" ".join(trailing[-5:]),
        )
        escalation: EscalationDecision | None = None
        if outcome.fired:
            escalation = outcome
            # Every signal fires AT MOST ONCE per session (live-caught 2026-08-18 for
            # cannot_answer: every below-floor turn of one call emailed the owner
            # again; the audit's cost review found high_value_lead re-firing on every
            # turn of a hot lead the same way). The turn's own state still reports the
            # escalation (LoopResult) — only the repeat row + repeat notification are
            # suppressed. One conversation is one business event per signal.
            fired_signal = (outcome.signal or Signal.cannot_answer).value
            suppress = await _session_already_escalated(
                self.session,
                admin_id=admin_id,
                session_id=session_id,
                signal=fired_signal,
            )
            if not suppress:
                await _record_escalation(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=(outcome.signal or Signal.cannot_answer).value,
                    gate="outcome",
                    score=outcome.score_or_confidence,
                    reasons=list(outcome.reasons),
                )

        # --- RESPOND (§3.4.2 channel arbiter) -------------------------------- #
        # Defense 4 (§3.4.14): pre-send check strips any platform-impersonation the
        # model may have produced and never lets the untrusted-data delimiters leak.
        reply_text = pre_send_check(resp.text)

        # --- FAITHFUL READBACK ENFORCEMENT (§3.3.4 rule 1 / §3.4.13, C4) ------ #
        action_results = [
            (call, r) for call, r in resp.dispatched if str(call.get("tool_id", "")) in ACTION_TOOLS
        ]
        ok_actions = [(c_, r) for c_, r in action_results if r.outcome is tool_contract.Outcome.ok]
        claimed_without_action = (
            action_results and not ok_actions and _SUCCESS_CLAIM.search(reply_text)
        ) or (not action_results and _FIRST_PERSON_SUCCESS_CLAIM.search(reply_text))
        # A reply composed in CODE below (the unfounded-claim or below-floor
        # replacement) is a fixed message, not a knowledge answer: persisted as such,
        # so the dashboard never asks the owner to review a canned line as "weakly
        # backed" (2026-09-06 E2E walk, E2E-6).
        deterministic_reply = False
        if claimed_without_action:
            # The model claimed an action no tool confirmed — attempted-and-failed, or
            # never attempted at all (F110) — replace with the honest failure ack and
            # make sure a human is flagged (dedupe-suppressed like any other
            # cannot_answer repeat).
            reply_text = _UNFOUNDED_CLAIM_REPLACEMENT
            deterministic_reply = True
            if escalation is None:
                escalation = EscalationDecision(True, Signal.cannot_answer, None, None)
                if not await _session_already_escalated(
                    self.session,
                    admin_id=admin_id,
                    session_id=session_id,
                    signal=Signal.cannot_answer.value,
                ):
                    await _record_escalation(
                        self.session,
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        resolved_lead_id=resolved_lead_id,
                        signal=Signal.cannot_answer.value,
                        gate="outcome",
                        score=None,
                    )
        elif ok_actions:
            # A confirmed action's numbers must come from its payload; on a mismatch,
            # append the deterministic confirmation built from what actually happened
            # rather than shipping an embellished readback (grounding.py guard's first
            # load-bearing caller).
            confirmed_fields: dict[str, object] = {}
            for _, r in ok_actions:
                confirmed_fields.update(r.payload)
            if not faithful_readback_ok(reply_text, confirmed_fields):
                summary = ", ".join(
                    f"{k}: {v}" for k, v in sorted(confirmed_fields.items()) if v is not None
                )
                reply_text = f"{reply_text}\n\nTo confirm the exact details: {summary}."
        if (
            not claimed_without_action
            and escalation is not None
            and escalation.signal is Signal.cannot_answer
            and not is_clarifying_question(reply_text)
        ):
            # Below the grounding floor the draft is a guess (2026-09-05 audit F100):
            # it used to ship anyway with "I've flagged this" appended, so the
            # visitor got a confident wrong answer AND a hand-off. An employee who
            # does not know says so and gets a person — the draft never leaves. A
            # bare clarifying question ("what's your email?") asserts nothing and
            # still goes out; an unfounded action claim keeps its own, more
            # specific replacement above.
            reply_text = await self._handoff_ack()
            deterministic_reply = True
        # Escalation-time contact ask — shared helper since C5; see _with_contact_ask.
        if escalation is not None:
            reply_text = self._with_contact_ask(
                reply_text,
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
            )
        # AI-identity disclosure backstop (§3.4.16): on the FIRST outbound of a new
        # conversation on a text channel, guarantee the disclosure in code rather than
        # trusting the model to include it. Surfaced once per conversation — a prior
        # luciel/human turn in history means it was already disclosed.
        prior_assistant_turn = any(t.role in ("luciel", "human_agent") for t in history)
        if not prior_assistant_turn and channel != "voice":
            reply_text = ensure_ai_identity_disclosure(
                reply_text,
                business_name=self.business_name,
                assistant_name=self.assistant_name,
            )
        requested_channel = channel_switch.detect_requested_channel(inbound_text)
        outbound = choose_outbound_channel(
            inbound_channel=channel,
            enabled_channels=self.enabled_channels or {channel},
            response_length=len(reply_text),
            customer_requested_channel=requested_channel,
            is_urgent_escalation=outcome.fired,
        )
        if (
            outbound != channel
            and escalation is None
            and outbound in channel_switch.KIND_FOR_CHANNEL
        ):
            if requested_channel == outbound and sess_obj is not None:
                # The customer ASKED for it there (round 6 WP-H, F060): a confirmed
                # address gets it now; otherwise ask where and park the answer.
                reply_text = await self._switch_or_ask(
                    sess_obj,
                    outbound=outbound,
                    reply_text=reply_text,
                    inbound_text=inbound_text,
                    resolved_lead_id=resolved_lead_id,
                )
            elif outbound in _CHANNEL_OFFER:
                # The arbiter would rather answer elsewhere (a long answer on SMS,
                # §3.4.2). Nothing is sent uninvited: the reply stays on the channel
                # the lead wrote on and OFFERS the switch.
                reply_text = reply_text.rstrip() + _CHANNEL_OFFER[outbound]
        reply_row = await persist_turn(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role="luciel",
            text=reply_text,
            # A fixed replacement is not scored: the score measured the DRAFT that
            # never left, and the evidence view would read it against a canned line.
            grounding_score=None if deterministic_reply else grounding.score,
            source_chunks=attributed_chunks,
            at=datetime.now(UTC),
            deterministic=deterministic_reply,
        )
        # Durable retrieval trace (§5.4 / C6): one row per knowledge SOURCE this
        # answer retrieved from (deduped — several chunks of one document are one
        # "question used this source" fact). Feeds the §3.9 top-sources metric and
        # the §3.2.2 delete-confirmation tooltip.
        if attributed_chunks:
            from app.models.trace import RetrievalTraceEvent  # noqa: PLC0415

            seen_sources: set[str] = set()
            for chunk in attributed_chunks:
                raw_source = str(chunk.get("source_id", "") or "")
                if not raw_source or raw_source in seen_sources:
                    continue
                seen_sources.add(raw_source)
                try:
                    source_uuid = uuid.UUID(raw_source)
                except ValueError:
                    continue
                self.session.add(
                    RetrievalTraceEvent(
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        transcript_id=reply_row.id,
                        source_id=source_uuid,
                    )
                )
        return LoopResult(
            reply_message_id=reply_row.id,
            reply_text=reply_text,
            outbound_channel=outbound,
            counted=counted,
            escalation=escalation,
            grounding_score=grounding.score,
            used_fallback=used_fallback,
            iterations=iterations,
            tool_outcomes=resp.tool_outcomes,
        )

    # --- internals ---------------------------------------------------------- #
    async def _turn_tool_defs(self) -> list[dict[str, Any]] | None:
        """The provider-neutral tool defs THIS turn may propose (C4).

        Intersection of: the session's frozen snapshot capability set (§3.8.7 rule E;
        None = older session → no snapshot filter), the instance's LIVE enabled
        authorizations (default-deny — an unauthorized tool is never even described
        to the model), and the ids the resolver can bind. None when no tools are
        wired — which keeps the call byte-identical to pre-tool-use behavior."""
        if self.tool_resolver is None:
            return None
        from app.models.tool_auth import InstanceToolAuthorization  # noqa: PLC0415

        rows = (
            await self.session.execute(
                select(InstanceToolAuthorization.tool_id).where(
                    InstanceToolAuthorization.admin_id == self.admin_id,
                    InstanceToolAuthorization.instance_id == self.instance_id,
                    InstanceToolAuthorization.enabled.is_(True),
                )
            )
        ).scalars()
        enabled = set(rows)
        if self.snapshot_tool_ids is not None:
            enabled &= self.snapshot_tool_ids
        enabled = {tid for tid in enabled if self.tool_resolver(tid) is not None}
        # A tool whose provider is not CONNECTED (or whose channel is off) is not
        # described to the model at all (2026-09-05 audit): the broker would refuse the
        # call anyway, but a model that was told it could book will promise to book.
        enabled = {tid for tid in enabled if await self._tool_operable(tid)}
        return build_tool_defs(enabled) if enabled else None

    async def _tool_operable(self, tool_id: str) -> bool:
        from app.connections import gate  # noqa: PLC0415
        from app.tools import catalog  # noqa: PLC0415

        tool = self.tool_resolver(tool_id) if self.tool_resolver is not None else None
        connection_type = (
            getattr(tool, "requires_connection", catalog.TOOL_CONNECTION_TYPE.get(tool_id))
            if tool is not None
            else catalog.TOOL_CONNECTION_TYPE.get(tool_id)
        )
        if connection_type is not None:
            status = await gate.connection_status(
                self.session,
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                connection_type=connection_type,
            )
            if status != "connected":
                return False
        needs_channel = {"send_email": "email", "send_sms": "sms"}.get(tool_id)
        if needs_channel is not None and self.enabled_channels:
            return needs_channel in self.enabled_channels
        return True

    @staticmethod
    def _tool_result_feedback(call: dict[str, Any], result: ToolResult) -> str:
        """The redacted, structured JSON the model reads back for one dispatch (C4).

        Carries what §3.3.4 says the loop must be able to distinguish: the outcome
        class, the confirming payload on ok, the candidate list on ambiguous
        (truncated), the NAMED missing fields on missing_input, and the non-secret
        reason on failed. Length-capped so a huge record cannot flood the context."""
        body: dict[str, Any] = {
            "tool": str(call.get("tool_id", "")),
            "outcome": result.outcome.value,
        }
        if result.outcome is tool_contract.Outcome.ok:
            body["payload"] = result.payload
        elif result.outcome is tool_contract.Outcome.ambiguous:
            candidates = list(result.payload.get("candidates") or [])
            body["candidates"] = candidates[:AMBIGUOUS_CANDIDATE_CAP]
            if len(candidates) > AMBIGUOUS_CANDIDATE_CAP:
                body["more_candidates"] = len(candidates) - AMBIGUOUS_CANDIDATE_CAP
        elif result.outcome is tool_contract.Outcome.missing_input:
            body["missing_fields"] = list(result.missing_fields)
        elif result.outcome is tool_contract.Outcome.empty:
            body["payload"] = result.payload
        else:  # failed
            body["reason"] = result.reason or "failed"
        text = json.dumps(body, default=str)
        if len(text) > TOOL_RESULT_CONTENT_CAP:
            text = text[:TOOL_RESULT_CONTENT_CAP] + '… (truncated)"}'
        return text

    async def _reason(
        self,
        inbound_text: str,
        history: list[Transcript],
        wrapped_chunks: list[str],
        *,
        resolved_lead_id: uuid.UUID | None = None,
        prior_summaries: list[str] | None = None,
        lead_context: str | None = None,
        system_prompt: str | None = None,
        tool_defs: list[dict[str, Any]] | None = None,
        session_id: uuid.UUID | None = None,
    ) -> tuple[_Reasoned, int, bool, bool]:
        """PLAN→ACT→REFLECT up to MAX_ITERATIONS (§3.4.1). Returns the final reasoned
        answer, the iteration count, whether the fallback provider was used, and
        whether BOTH providers failed (llm_failed).

        Termination (C4): the loop ends when a response proposes NO tool calls —
        natural completion — or at the iteration bound. Tool results are fed back as
        structured neutral ``tool_result`` turns (payloads, named missing fields,
        candidate lists — §3.3.4), never as bare outcome-class strings."""
        messages: list[dict[str, Any]] = []
        if lead_context:
            # Round 5C item 6: the lead's own RECORD (name/interest), so Luciel
            # stops re-asking facts it has on file ("never re-explains
            # themselves", Journey §7). Untrusted — it derives from lead-authored
            # content — and it deliberately carries NO contact identifiers: a
            # matching identifier never authorizes PII readback (§3.4.9); the
            # lead must re-state contact details themselves.
            messages.append({"role": "user", "content": wrap_untrusted(lead_context)})
        if prior_summaries:
            # §3.4.6 cross-session memory, injected BEFORE this session's history.
            # Summaries derive from lead-authored content, so they enter the context
            # as untrusted data (§3.4.14 Defense 2), never as instructions.
            messages.append(
                {
                    "role": "user",
                    "content": wrap_untrusted(
                        "Prior conversation summaries:\n" + "\n".join(prior_summaries)
                    ),
                }
            )
        for t in history:
            role = "user" if t.role == "lead" else "assistant"
            messages.append({"role": role, "content": t.text})
        if wrapped_chunks:
            messages.append({"role": "user", "content": "Knowledge:\n" + "\n".join(wrapped_chunks)})

        prompt = system_prompt if system_prompt is not None else self.system_prompt
        used_fallback = False
        tool_outcomes: list[str] = []
        dispatched: list[tuple[dict[str, Any], ToolResult]] = []
        last: LLMResponse | None = None
        iterations = 0
        for _ in range(MAX_ITERATIONS):
            iterations += 1
            try:
                resp = await self.router.complete(
                    prompt,
                    messages,
                    tools=tool_defs,
                    **routing_hints(inbound_text, prompt, messages, tool_defs),
                )
            except ProviderError:
                # Router already failed primary→fallback; a ProviderError here means
                # BOTH providers are down (§3.4.3 immediate failover, no retry).
                return (
                    _Reasoned("", 0.0, tool_outcomes, dispatched),
                    iterations,
                    used_fallback,
                    True,
                )
            used_fallback = used_fallback or resp.used_fallback
            last = resp
            if not resp.tool_calls:
                # Natural completion: the model answered without (further) tool use.
                break
            # ACT: dispatch the proposed calls via the Arc 12 broker contract, then
            # REFLECT: append the assistant's tool turn + one structured tool_result
            # turn per call (untrusted-wrapped — results embed lead/provider content).
            results = await self._dispatch_tools(
                resp.tool_calls, resolved_lead_id=resolved_lead_id, session_id=session_id
            )
            dispatched.extend(results)
            tool_outcomes.extend(r.outcome.value for _, r in results)
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.text,
                    "tool_calls": resp.tool_calls,
                }
            )
            for call, result in results:
                messages.append(
                    {
                        "role": "tool_result",
                        "call_id": str(call.get("call_id", "")),
                        "tool_id": str(call.get("tool_id", "")),
                        "content": wrap_untrusted(self._tool_result_feedback(call, result)),
                        "is_error": result.outcome is tool_contract.Outcome.failed,
                    }
                )

        if last is None:
            # Unreachable: MAX_ITERATIONS >= 1 so the loop always assigns or returns.
            raise RuntimeError("reasoning produced no response")
        return (
            _Reasoned(last.text, last.confidence, tool_outcomes, dispatched),
            iterations,
            used_fallback,
            False,
        )

    async def _dispatch_tools(
        self,
        tool_calls: list[dict[str, object]],
        *,
        resolved_lead_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
    ) -> list[tuple[dict[str, Any], ToolResult]]:
        """Dispatch PLAN tool intents via the Arc 12 broker, returning each proposed
        call paired with its full five-outcome result (§3.3.3/§3.3.4). The broker
        enforces default-deny authorization + connection gating in code — the loop
        never trusts the model to self-authorize a tool (§3.4.14).

        The two loop-side refusals carry DISTINCT reasons (C4 harmony fix — both
        used to read as a bare "failed", indistinguishable from a provider error):
        ``tool_not_in_snapshot`` for the §3.8.7 rule-E capability gate, and
        ``tool_unavailable`` for an id the resolver cannot bind."""
        from app.tools.broker import ToolBroker

        broker = ToolBroker(self.session)
        results: list[tuple[dict[str, Any], ToolResult]] = []
        for call in tool_calls:
            tool_id = str(call.get("tool_id", ""))
            inputs = call.get("inputs", {})
            # §3.8.7 rule E capability gate: a tool enabled AFTER this conversation
            # opened is not part of its frozen snapshot capability set, so it is
            # unavailable to THIS conversation (applies to the next one). Deferred
            # additive change → structured `failed`, never a silent success-claim
            # (§3.3.4 rule 1). None snapshot = older session → skip this filter.
            if self.snapshot_tool_ids is not None and tool_id not in self.snapshot_tool_ids:
                results.append((dict(call), tool_contract.failed("tool_not_in_snapshot")))
                continue
            tool = self.tool_resolver(tool_id) if self.tool_resolver else None
            if tool is None:
                # No backend wired (Arc 17) or unknown tool → structured failure,
                # never a silent success-claim (§3.3.4 rule 1).
                results.append((dict(call), tool_contract.failed("tool_unavailable")))
                continue
            result = await broker.dispatch(
                tool,
                dict(inputs) if isinstance(inputs, dict) else {},
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                resolved_lead_id=resolved_lead_id,
                session_id=session_id,
            )
            results.append((dict(call), result))
        return results

    @staticmethod
    def _with_contact_ask(
        reply_text: str,
        *,
        channel: str,
        resolved_lead_id: uuid.UUID | None,
        transport_identifier: str | None,
    ) -> str:
        """Append the contact ask for an anonymous escalated visitor (C5).

        Owner-directed 2026-08-18, extended audit round 3: when a turn escalates on a
        channel that carries NO transport identity and no lead is on file, the team
        has been flagged about someone they cannot reach — so ask, deterministically,
        in code. Previously only the gate-2 path asked; gate-1 (the most likely
        trigger: "can I talk to a human"), moderation, at-cap, and llm_unavailable
        all escalated an anonymous widget visitor without asking. Strong channels
        (voice/SMS/email/Meta) skip this: the transport already names the person."""
        if resolved_lead_id is None and transport_identifier is None and channel == "widget":
            return (
                f"{reply_text}\n\nSo the team can reach you, what's your name and "
                "the best email or phone number to contact you at?"
            )
        return reply_text

    async def _handoff_ack(self) -> str:
        """The customer-facing handoff acknowledgement (harmony fix 2026-08-04 item 4;
        rewritten after the 2026-08-17 live run).

        NEITHER branch may claim a real-time connection. Even with an escalation
        contact configured (§3.5.3), what actually happens is a notification to
        that contact — a human sees it when they see it. The doctrine
        (app/notifications/CLAUDE.md, root CLAUDE.md trust invariants) is explicit:
        say the request was PASSED to the team, never "connecting you right now" —
        and the first live widget conversation served exactly the forbidden
        phrasing from the configured branch of this very method. The branches now
        differ only in how much reach the platform can honestly claim.
        """
        inst = (
            await self.session.execute(
                select(Instance).where(
                    Instance.admin_id == self.admin_id, Instance.id == self.instance_id
                )
            )
        ).scalar_one_or_none()
        configured = has_configured_escalation_contact(inst.escalation if inst else None)
        # Round 6 WP-E (F054): when the owner described their team's hours and nobody
        # is reachable now, say WHEN a person follows up instead of "shortly". Luciel
        # itself keeps answering; only the promise about a human changes.
        when = follow_up_phrase(
            parse_team_availability(
                inst.team_availability if inst else None, inst.timezone if inst else None
            )
        )
        if when is not None:
            opener = (
                "Of course — I've flagged this for the team."
                if configured
                else "I've passed this along to the team."
            )
            return f"{opener} They're away right now, so someone will follow up with you {when}."
        if configured:
            return (
                "Of course — I've flagged this for the team, and they'll follow up "
                "with you shortly."
            )
        return "I've passed this along to the team — they'll follow up with you shortly."

    async def _hand_off_with_ack(
        self,
        *,
        session_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None,
        channel: str,
        signal: Signal,
        gate: str,
        score: float | None,
        ack: str,
        reasons: tuple[str, ...] = (),
    ) -> LoopResult:
        """Acknowledge to the customer, record the escalation, and set the session
        human_controlled (§3.4.12 Luciel-initiated handoff). The session counts as
        one budget unit (§3.4.1b)."""
        await _record_escalation(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal.value,
            gate=gate,
            score=score,
            reasons=list(reasons),
        )
        ack_row = await persist_turn(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            role="luciel",
            text=ack,
            at=datetime.now(UTC),
            deterministic=True,
        )
        await handoff.take_over(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            actor_user_id=None,
            luciel_initiated=True,
        )
        return LoopResult(
            reply_message_id=ack_row.id,
            reply_text=ack,
            outbound_channel=channel,
            counted=True,
            handed_off=True,
            escalation=EscalationDecision(True, signal, None, score),
        )

    async def _count_served_turn(self, session_id: uuid.UUID) -> bool:
        """The fire-once budget counter for a SERVED turn (§3.4.1b), with the arrears
        usage report and the block notices that ride on the count moving."""
        counted = await budget.count_session_once(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
        )
        if counted:
            # PAYG usage is billed in arrears from this counter (Vision §7): report the
            # new block to Stripe as it opens, so overage past the free 50 is actually
            # billed rather than only computed.
            from app.billing import metering, notice_delivery  # noqa: PLC0415 — import cycle

            await metering.report_usage_after_count(
                self.session, admin_id=self.admin_id, instance_id=self.instance_id
            )
            # The block-start / 80% notices are evaluated the moment the count moves
            # (audit F011), not only by the daily sweep. Never raises.
            await notice_delivery.send_usage_notices_after_count(
                self.session, admin_id=self.admin_id, now=datetime.now(UTC)
            )
        return counted

    # --- channel switching with contact confirmation (round 6 WP-H, F060) --------- #

    async def _deliver_elsewhere(self, outbound: str, to: str, body: str) -> bool:
        """Send ``body`` to a CONFIRMED address on ``outbound`` as the tenant; audited
        either way; never raises for a delivery problem."""
        from app.channels.outbound import send_outbound_reply  # noqa: PLC0415

        result = await send_outbound_reply(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            channel=outbound,
            to=to,
            body=body,
            subject="A message from your assistant" if outbound == "email" else None,
        )
        await audit.emit(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            actor_type=ActorType.platform,
            event_type="channel_switch_delivery",
            payload={"channel": outbound, "delivered": result.delivered, "detail": result.detail},
        )
        return result.delivered

    async def _unconfirmed_candidate(self, lead_id: uuid.UUID | None, kind: str) -> str | None:
        """An address on the lead's own record that was never confirmed — offered back
        as a question, never used on its own."""
        if lead_id is None:
            return None
        from app.models.lead import Lead  # noqa: PLC0415
        from app.runtime.lead_capture import identifier_kind  # noqa: PLC0415

        lead = (
            await self.session.execute(
                select(Lead).where(Lead.admin_id == self.admin_id, Lead.id == lead_id)
            )
        ).scalar_one_or_none()
        if lead is None:
            return None
        for value in (lead.email if kind == "email" else lead.phone, lead.contact_identifier):
            if value and identifier_kind(value) == kind:
                return str(value)
        return None

    async def _switch_or_ask(
        self,
        sess_obj: Session,
        *,
        outbound: str,
        reply_text: str,
        inbound_text: str,
        resolved_lead_id: uuid.UUID | None,
    ) -> str:
        """The customer asked for this answer on ``outbound``. A confirmed address gets
        it now (with a same-channel note); otherwise ask where, and park the answer."""
        kind = channel_switch.KIND_FOR_CHANNEL[outbound]
        typed = channel_switch.identifier_for(kind, inbound_text)
        to = typed or await channel_switch.confirmed_identifier(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            lead_id=resolved_lead_id,
            kind=kind,
        )
        if to is not None:
            if typed and resolved_lead_id is not None:
                await channel_switch.confirm_identifier(
                    self.session,
                    admin_id=self.admin_id,
                    instance_id=self.instance_id,
                    lead_id=resolved_lead_id,
                    kind=kind,
                    value=typed,
                    source="volunteered",
                )
            delivered = await self._deliver_elsewhere(outbound, to, reply_text)
            note = (
                channel_switch.also_sent_line(outbound, to)
                if delivered
                else channel_switch.failed_line(outbound)
            )
            return f"{reply_text.rstrip()}\n\n{note}"
        candidate = await self._unconfirmed_candidate(resolved_lead_id, kind)
        sess_obj.pending_outbound = channel_switch.new_pending(
            outbound, reply_text, candidate
        ).as_json()
        await self.session.flush()
        return f"{reply_text.rstrip()}\n\n{channel_switch.ask_line(outbound, candidate)}"

    async def _settle_pending_outbound(
        self,
        sess_obj: Session,
        *,
        session_id: uuid.UUID,
        channel: str,
        inbound_text: str,
        resolved_lead_id: uuid.UUID | None,
    ) -> LoopResult | None:
        """This turn answers "where should I send it?". A typed address or a yes to the
        offered one sends the parked answer; a no drops it; anything else means the
        customer moved on — the parked answer is dropped and the loop runs normally."""
        pending = channel_switch.PendingOutbound.from_json(sess_obj.pending_outbound)
        sess_obj.pending_outbound = None
        await self.session.flush()
        if pending is None:
            return None
        kind = channel_switch.KIND_FOR_CHANNEL[pending.channel]
        typed = channel_switch.identifier_for(kind, inbound_text)
        to = typed or (pending.candidate if channel_switch.is_confirmation(inbound_text) else None)
        if to is None and not channel_switch.is_decline(inbound_text):
            return None
        if to is None:
            reply = channel_switch.DECLINED_LINE
        else:
            if resolved_lead_id is not None:
                await channel_switch.confirm_identifier(
                    self.session,
                    admin_id=self.admin_id,
                    instance_id=self.instance_id,
                    lead_id=resolved_lead_id,
                    kind=kind,
                    value=to,
                    source="volunteered",
                )
            delivered = await self._deliver_elsewhere(pending.channel, to, pending.body)
            reply = (
                channel_switch.sent_line(pending.channel, to)
                if delivered
                else f"{channel_switch.failed_line(pending.channel)}\n\n{pending.body}"
            )
        row = await persist_turn(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            role="luciel",
            text=reply,
            at=datetime.now(UTC),
            deterministic=True,
        )
        counted = await self._count_served_turn(session_id)
        return LoopResult(
            reply_message_id=row.id, reply_text=reply, outbound_channel=channel, counted=counted
        )

    async def _emit_budget_exhausted(
        self, session_id: uuid.UUID, resolved_lead_id: uuid.UUID | None
    ) -> None:
        # cannot_answer with reason budget_exhausted (§3.4.1b at-cap behavior). Once
        # per session: an at-cap tenant's visitor sending twenty messages is one
        # escalation and one owner notification, not twenty (cost review).
        if await _session_already_escalated(
            self.session,
            admin_id=self.admin_id,
            session_id=session_id,
            signal="budget_exhausted",
        ):
            return
        await _record_escalation(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal="budget_exhausted",
            gate="intake",
            score=None,
        )

    async def _emit_llm_unavailable(
        self, session_id: uuid.UUID, resolved_lead_id: uuid.UUID | None
    ) -> None:
        # Once per session, same reasoning as budget_exhausted: an outage is one
        # event for the conversation, however many turns it spans.
        if await _session_already_escalated(
            self.session,
            admin_id=self.admin_id,
            session_id=session_id,
            signal="llm_unavailable",
        ):
            return
        await _record_escalation(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal="llm_unavailable",
            gate="outcome",
            score=None,
        )

    async def _audit_moderation(self, session_id: uuid.UUID, category: str) -> None:
        await audit.emit(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            event_type="content_moderation_event",
            actor_type=ActorType.luciel_runtime,
            payload={"category": category, "session_id": str(session_id)},
        )

    async def _audit_injection(self, session_id: uuid.UUID) -> None:
        await audit.emit(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            event_type="prompt_injection_detected",
            actor_type=ActorType.luciel_runtime,
            payload={"session_id": str(session_id)},
        )
        # The detection is monitoring-only for THIS turn (§3.4.14), but a source that
        # keeps probing is abuse: it feeds the same per-source auto-block a flood does
        # (F086), so the fiftieth attempt is refused at the edge, before any LLM call.
        if self.abuse_source is not None:
            from app.channels import rate_limit  # noqa: PLC0415

            channel, source_id = self.abuse_source
            rate_limit.get_store().record_abuse(channel, source_id)


def routing_hints(
    inbound_text: str,
    prompt: str,
    messages: list[dict[str, Any]],
    tool_defs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The fast/cheap routing hints for one model call (§3.4.3 / §7 #9).

    The router's gate needs all three — no tools, a small context, low complexity —
    and the orchestrator never passed any, so the bound fast provider was dead weight
    (cost, 2026-09-05 audit). A social turn (thanks / goodbye / a bare greeting) has
    no factual claim to ground and nothing for a tool to do: that is the honest fast
    lane. Everything else keeps the primary model.
    """
    social = is_conversational_turn(inbound_text)
    chars = len(prompt) + sum(len(str(m.get("content", ""))) for m in messages)
    return {
        "requires_tools": bool(tool_defs) and not social,
        "context_tokens": chars // 4,
        "complexity": 0.2 if social else 1.0,
    }


@dataclass
class _Reasoned:
    text: str
    confidence: float
    tool_outcomes: list[str]
    # Every (proposed call, broker result) pair this turn dispatched, in order —
    # run_turn's ask-bound and faithful-readback enforcement read these (C4).
    dispatched: list[tuple[dict[str, Any], ToolResult]] = field(default_factory=list)
