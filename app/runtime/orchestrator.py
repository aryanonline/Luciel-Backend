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
      → ESCALATION GATE 2 — OUTCOME (§3.4.5): cannot_answer (low confidence AND below
        the 0.50 grounding floor with no resolving tool) or high_value_lead.

  Half B — emission:
    RESPOND: channel arbiter picks the outbound channel (§3.4.2).
    FINALIZATION: persist transcript; the summary is produced at session close
      (§3.4.7), not per message — the loop persists turns, summarization fires on
      session end (memory.summarize / persist_summary).

The LLM's instructions are NOT a security boundary (§3.4.14 / §7 #36): tenant
isolation (RLS), tool authorization (broker default-deny), and budget all hold in
code regardless of what the model is convinced to say.

FLAGGED: escalation NOTIFICATION DELIVERY (admin email/SMS routing, §3.5) is Arc 15
— here an escalation writes the escalation_events row + the audit event; the
delivery worker binds later. Vector RETRIEVAL is Arc 11 and graph/hybrid is Arc 16
— the loop calls a retrieval callable injected by the caller so it is testable now
and the production retriever binds behind the same signature.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Session, Transcript
from app.runtime import budget, handoff, memory
from app.runtime.channel_arbiter import choose_outbound_channel
from app.runtime.escalation import EscalationDecision, Signal, evaluate_intake, evaluate_outcome
from app.runtime.grounding import score_grounding
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
from app.tools.contract import Tool

MAX_ITERATIONS = 5  # §3.4.1 — internal cost-control constant, identical on every tier.

# Retriever: (query) -> list of retrieved chunk texts. Vector impl lands Arc 11,
# graph/hybrid Arc 16 (§3.2). Injected so the loop is testable now.
Retriever = Callable[[str], Awaitable[list[str]]]

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


async def _persist_turn(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    text: str,
) -> None:
    session.add(
        Transcript(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role=role,
            text=text,
        )
    )
    await session.flush()


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
    """Write the escalation_events row + the audit event (§3.5.2 / §5.2).

    Notification delivery to the admin's configured channel is Arc 15 (§3.5) —
    flagged in the module docstring."""
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
    system_prompt: str = field(default="", init=False)
    has_payment_method: bool = False
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
        self.system_prompt = build_system_prompt(self.business_context, self.luciel_email_address)

    async def run_turn(
        self,
        *,
        session_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None,
        channel: str,
        mode: str,
        inbound_text: str,
        transport_identifier: str | None = None,
    ) -> LoopResult:
        """Run one inbound message through the loop. `mode` is the session's current
        conversation mode (ai | human_controlled). `transport_identifier` is the
        channel's stable sender key (phone/email/meta-id) used for lead capture +
        identity resolution (§3.4.4/§3.4.9); None for the weak widget channel."""
        admin_id, instance_id = self.admin_id, self.instance_id

        # Human-controlled sessions STOP the loop (§3.4.12): persist the inbound for
        # the dashboard live feed, make no LLM call.
        if not handoff.loop_should_run(mode):
            await _persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="lead",
                text=inbound_text,
            )
            return LoopResult(
                reply_text="",
                outbound_channel=channel,
                counted=False,
                handed_off=True,
            )

        await _persist_turn(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role="lead",
            text=inbound_text,
        )

        # --- CONVERSATION BUDGET GATE (§3.4.1b) — before PLAN ----------------- #
        budget_state = await budget.check_budget(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            has_payment_method=self.has_payment_method,
        )
        if budget_state.at_cap:
            # Graceful single-turn reply WITHOUT an LLM call; cannot_answer with
            # reason budget_exhausted; counter does NOT increment (§3.4.1b).
            await self._emit_budget_exhausted(session_id, resolved_lead_id)
            await _persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=AT_CAP_REPLY,
            )
            return LoopResult(
                reply_text=AT_CAP_REPLY,
                outbound_channel=channel,
                counted=False,
                at_cap=True,
                escalation=EscalationDecision(True, Signal.cannot_answer),
            )

        # --- LEAD CAPTURE COGNITION (§3.4.4 / §3.4.9) — always on ------------ #
        # Write/maintain the platform-owned Lead record and promote a weak widget
        # token to a real lead once a strong identifier is volunteered. The returned
        # id (if any) is threaded through escalation payloads + finalization so the
        # dashboard leads view and escalation notifications are actually populated.
        sess_obj = (
            await self.session.execute(
                select(Session).where(
                    Session.admin_id == admin_id, Session.id == session_id
                )
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
                resolved_lead_id = captured

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
                ack=(
                    "I want to make sure you're properly helped — connecting you with a person now."
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
                ack="Of course — I'm connecting you with someone from the team right now.",
            )

        # --- RETRIEVE (§3.2) — wrap as UNTRUSTED before the model (§3.4.14) --- #
        raw_chunks = await self.retriever(inbound_text)
        wrapped_chunks = [wrap_untrusted(c) for c in raw_chunks]

        # --- PLAN → ACT → REFLECT × up to 5 (§3.4.1) ------------------------- #
        resp, iterations, used_fallback, llm_failed = await self._reason(
            inbound_text, history, wrapped_chunks, resolved_lead_id=resolved_lead_id
        )

        if llm_failed:
            # Both providers down → llm_unavailable escalation (§3.4.5 server signal).
            await self._emit_llm_unavailable(session_id, resolved_lead_id)
            ack = (
                "I'm having a brief technical issue — I've flagged this so a person "
                "can follow up with you shortly."
            )
            await _persist_turn(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                role="luciel",
                text=ack,
            )
            # The first model-call attempt is the fire-once trigger even on failure?
            # No: §3.4.1b ties the counter to a model CALL succeeding/serving. A pure
            # provider outage with no served turn does not count. Flagged: revisit
            # with metering in Arc 18.
            return LoopResult(
                reply_text=ack,
                outbound_channel=channel,
                counted=False,
                escalation=EscalationDecision(True, Signal.cannot_answer),
                iterations=iterations,
            )

        # First model call succeeded → fire-once budget counter (§3.4.1b).
        counted = await budget.count_session_once(
            self.session, admin_id=admin_id, instance_id=instance_id, session_id=session_id
        )

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
        # AI-identity disclosure backstop (§3.4.16): on the FIRST outbound of a new
        # conversation on a text channel, guarantee the disclosure in code rather than
        # trusting the model to include it. Surfaced once per conversation — a prior
        # luciel/human turn in history means it was already disclosed.
        prior_assistant_turn = any(
            t.role in ("luciel", "human_agent") for t in history
        )
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
        await _persist_turn(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            role="luciel",
            text=reply_text,
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
    async def _reason(
        self,
        inbound_text: str,
        history: list[Transcript],
        wrapped_chunks: list[str],
        *,
        resolved_lead_id: uuid.UUID | None = None,
    ) -> tuple[_Reasoned, int, bool, bool]:
        """PLAN→ACT→REFLECT up to MAX_ITERATIONS (§3.4.1). Returns the final reasoned
        answer, the iteration count, whether the fallback provider was used, and
        whether BOTH providers failed (llm_failed)."""
        messages: list[dict[str, str]] = []
        for t in history:
            role = "user" if t.role == "lead" else "assistant"
            messages.append({"role": role, "content": t.text})
        if wrapped_chunks:
            messages.append({"role": "user", "content": "Knowledge:\n" + "\n".join(wrapped_chunks)})

        used_fallback = False
        tool_outcomes: list[str] = []
        last: LLMResponse | None = None
        iterations = 0
        for _ in range(MAX_ITERATIONS):
            iterations += 1
            try:
                resp = await self.router.complete(self.system_prompt, messages)
            except ProviderError:
                # Router already failed primary→fallback; a ProviderError here means
                # BOTH providers are down (§3.4.3 immediate failover, no retry).
                return _Reasoned("", 0.0, tool_outcomes), iterations, used_fallback, True
            used_fallback = used_fallback or resp.used_fallback
            last = resp
            # ACT: dispatch any proposed tool calls via the Arc 12 broker contract.
            if resp.tool_calls:
                outcomes = await self._dispatch_tools(
                    resp.tool_calls, resolved_lead_id=resolved_lead_id
                )
                tool_outcomes.extend(outcomes)
                # REFLECT: feed tool outcomes back as untrusted context, iterate.
                messages.append(
                    {
                        "role": "user",
                        "content": wrap_untrusted("Tool outcomes: " + ",".join(outcomes)),
                    }
                )
                if all(o == "ok" for o in outcomes):
                    break
                continue
            break

        if last is None:
            # Unreachable: MAX_ITERATIONS >= 1 so the loop always assigns or returns.
            raise RuntimeError("reasoning produced no response")
        return (
            _Reasoned(last.text, last.confidence, tool_outcomes),
            iterations,
            used_fallback,
            False,
        )

    async def _dispatch_tools(
        self,
        tool_calls: list[dict[str, object]],
        *,
        resolved_lead_id: uuid.UUID | None = None,
    ) -> list[str]:
        """Dispatch PLAN tool intents via the Arc 12 broker, returning the
        five-outcome class per call (§3.3.3/§3.3.4). The broker enforces
        default-deny authorization + connection gating in code — the loop never
        trusts the model to self-authorize a tool (§3.4.14)."""
        from app.tools.broker import ToolBroker

        broker = ToolBroker(self.session)
        outcomes: list[str] = []
        for call in tool_calls:
            tool_id = str(call.get("tool_id", ""))
            inputs = call.get("inputs", {})
            # §3.8.7 rule E capability gate: a tool enabled AFTER this conversation
            # opened is not part of its frozen snapshot capability set, so it is
            # unavailable to THIS conversation (applies to the next one). Deferred
            # additive change → structured `failed`, never a silent success-claim
            # (§3.3.4 rule 1). None snapshot = older session → skip this filter.
            if self.snapshot_tool_ids is not None and tool_id not in self.snapshot_tool_ids:
                outcomes.append("failed")
                continue
            tool = self.tool_resolver(tool_id) if self.tool_resolver else None
            if tool is None:
                # No backend wired (Arc 17) or unknown tool → structured failure,
                # never a silent success-claim (§3.3.4 rule 1).
                outcomes.append("failed")
                continue
            result = await broker.dispatch(
                tool,
                dict(inputs) if isinstance(inputs, dict) else {},
                admin_id=self.admin_id,
                instance_id=self.instance_id,
                resolved_lead_id=resolved_lead_id,
            )
            outcomes.append(result.outcome.value)
        return outcomes

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
        await _persist_turn(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            role="luciel",
            text=ack,
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
