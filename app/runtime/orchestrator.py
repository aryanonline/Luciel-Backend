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
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Session, Transcript
from app.models.instance import Instance
from app.notifications.escalation_dispatch import (
    has_configured_escalation_contact,
    notify_escalation_safely,
)
from app.runtime import budget, handoff, memory
from app.runtime.channel_arbiter import choose_outbound_channel
from app.runtime.escalation import EscalationDecision, Signal, evaluate_intake, evaluate_outcome
from app.runtime.grounding import faithful_readback_ok, score_grounding
from app.runtime.input_safety import (
    build_system_prompt,
    detect_injection,
    ensure_ai_identity_disclosure,
    moderate,
    pre_send_check,
    wrap_untrusted,
)
from app.runtime.lead_capture import capture_or_update_lead
from app.runtime.llm_router import LLMResponse, LLMRouter, ProviderError
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

# Conservative success-claim detector (§3.3.4 rule 1 backstop): verbs a reply may not
# use about an ACTION tool when no action returned `ok` this turn.
_SUCCESS_CLAIM = re.compile(
    r"\b(booked|scheduled|rescheduled|cancelled|canceled|confirmed|sent|delivered|"
    r"added to (?:the|your) calendar|updated your)\b",
    re.IGNORECASE,
)

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
    arrival time for the inbound row, wall-clock persist time for the reply."""
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
) -> None:
    """Write the escalation_events row + the audit event, then notify (§3.5.2 / §5.2).

    The row is written and flushed FIRST: it is the authoritative record and delivery is
    best-effort against it (§3.5.2). Notification never blocks or breaks the lead's turn
    (§3.5.4)."""
    session.add(
        EscalationEvent(
            escalation_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score_or_confidence=score,
        )
    )
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
    # §3.8.7 rule E (Decision #39): the CAPABILITY set of tools enabled when THIS
    # conversation began, taken from the session's config_snapshot. A tool enabled
    # AFTER open is an additive change that must apply to the NEXT conversation, not
    # this one — so a proposed tool_id outside this set is treated as unavailable even
    # if the live instance now authorizes it. None = no snapshot (older sessions) →
    # fall back to the broker's live gates alone. This is CAPABILITY=snapshot; the
    # broker's connection/authorization gates remain LIVE (usability=live, safety wins).
    snapshot_tool_ids: set[str] | None = None

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
            await self._emit_budget_exhausted(session_id, resolved_lead_id)
            at_cap_reply = self._with_contact_ask(
                AT_CAP_REPLY,
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
            )
            await persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=at_cap_reply,
                at=datetime.now(UTC),
            )
            return LoopResult(
                reply_text=at_cap_reply,
                outbound_channel=channel,
                counted=False,
                at_cap=True,
                # The decision names the SAME signal the escalation row records (C4
                # harmony fix — it used to say cannot_answer while the row said
                # budget_exhausted).
                escalation=EscalationDecision(True, Signal.budget_exhausted),
            )

        # --- CONTENT MODERATION (§3.4.15) ------------------------------------ #
        mod = moderate(inbound_text)
        if mod.should_escalate:
            await self._audit_moderation(session_id, mod.category.value)
            return await self._hand_off_with_ack(
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                channel=channel,
                signal=Signal.explicit_human_request,
                gate="intake",
                score=None,
                ack=self._with_contact_ask(
                    await self._handoff_ack(),
                    channel=channel,
                    resolved_lead_id=resolved_lead_id,
                    transport_identifier=transport_identifier,
                ),
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
                ack=self._with_contact_ask(
                    await self._handoff_ack(),
                    channel=channel,
                    resolved_lead_id=resolved_lead_id,
                    transport_identifier=transport_identifier,
                ),
            )

        # --- RETRIEVE (§3.2) — wrap as UNTRUSTED before the model (§3.4.14) --- #
        raw_chunks = await self.retriever(inbound_text)
        wrapped_chunks = [wrap_untrusted(c) for c in raw_chunks]
        # Harmony wave 2, item 2a: real chunk attribution for persistence, kept
        # separate from `raw_chunks` (which only ever feeds the model/scorer).
        # None when this construction site has no attribution source bound (voice,
        # bare fakes in tests) -- persisted as attribution_unavailable, never
        # fabricated. When bound, an empty list here is a REAL "retrieval found
        # nothing", distinct from None.
        attributed_chunks: list[dict[str, object]] | None = None
        if self.chunk_attributor is not None:
            attributed_chunks = await self.chunk_attributor(inbound_text)

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
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            session_id=session_id,
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
            await persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=ack,
                at=datetime.now(UTC),
            )
            # The first model-call attempt is the fire-once trigger even on failure?
            # No: §3.4.1b ties the counter to a model CALL succeeding/serving. A pure
            # provider outage with no served turn does not count. Flagged: revisit
            # with metering in Arc 18.
            return LoopResult(
                reply_text=ack,
                outbound_channel=channel,
                counted=False,
                # Same-signal harmony as the persisted llm_unavailable row (C4).
                escalation=EscalationDecision(True, Signal.llm_unavailable),
                iterations=iterations,
            )

        # First model call succeeded → fire-once budget counter (§3.4.1b).
        counted = await budget.count_session_once(
            self.session, admin_id=admin_id, instance_id=instance_id, session_id=session_id
        )
        if counted:
            # PAYG usage is billed in arrears from this counter (Vision §7): report the
            # new block to Stripe as it opens, so overage past the free 50 is actually
            # billed rather than only computed.
            from app.billing import metering  # noqa: PLC0415 — avoids an import cycle

            await metering.report_usage_after_count(
                self.session, admin_id=admin_id, instance_id=instance_id
            )

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
        tool_succeeded = any(o == "ok" for o in resp.tool_outcomes)
        grounding = score_grounding(resp.text, raw_chunks, tool_succeeded)

        # --- ESCALATION GATE 2 — OUTCOME (§3.4.5) ---------------------------- #
        outcome = evaluate_outcome(
            message=inbound_text,
            plan_confidence=resp.confidence,
            grounding_score=grounding.score,
            resolving_tool_succeeded=tool_succeeded,
        )
        escalation: EscalationDecision | None = None
        if outcome.fired:
            escalation = outcome
            # cannot_answer fires AT MOST ONCE per session (live-caught 2026-08-18:
            # every below-floor turn of one call emailed the owner again). The turn's
            # own state still reports the escalation (LoopResult) — only the repeat
            # row + repeat notification are suppressed. high_value_lead, budget, and
            # llm_unavailable are NOT deduped: each is a fresh business event.
            suppress = outcome.signal == Signal.cannot_answer and await _session_already_escalated(
                self.session,
                admin_id=admin_id,
                session_id=session_id,
                signal=Signal.cannot_answer.value,
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
        if action_results and not ok_actions and _SUCCESS_CLAIM.search(reply_text):
            # The model claimed an action no tool confirmed — replace with the honest
            # failure ack and make sure a human is flagged (dedupe-suppressed like any
            # other cannot_answer repeat).
            reply_text = _UNFOUNDED_CLAIM_REPLACEMENT
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
        outbound = choose_outbound_channel(
            inbound_channel=channel,
            enabled_channels=self.enabled_channels or {channel},
            response_length=len(reply_text),
            is_urgent_escalation=outcome.fired,
        )
        reply_row = await persist_turn(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role="luciel",
            text=reply_text,
            grounding_score=grounding.score,
            source_chunks=attributed_chunks,
            at=datetime.now(UTC),
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
        return build_tool_defs(enabled) if enabled else None

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
                resp = await self.router.complete(prompt, messages, tools=tool_defs)
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
        )
        await persist_turn(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            role="luciel",
            text=ack,
            at=datetime.now(UTC),
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
            reply_text=ack,
            outbound_channel=channel,
            counted=True,
            handed_off=True,
            escalation=EscalationDecision(True, signal, None, score),
        )

    async def _emit_budget_exhausted(
        self, session_id: uuid.UUID, resolved_lead_id: uuid.UUID | None
    ) -> None:
        # cannot_answer with reason budget_exhausted (§3.4.1b at-cap behavior).
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


@dataclass
class _Reasoned:
    text: str
    confidence: float
    tool_outcomes: list[str]
    # Every (proposed call, broker result) pair this turn dispatched, in order —
    # run_turn's ask-bound and faithful-readback enforcement read these (C4).
    dispatched: list[tuple[dict[str, Any], ToolResult]] = field(default_factory=list)
