"""Agentic loop, budget gate, escalation wiring (DOCTRINE ANCHOR — Architecture §3.4.1).

The loop, per inbound message:

  Half A — intake:
    RECEIVE
      → CONVERSATION BUDGET GATE (§3.4.1b): at-cap with no payment method → graceful
        single-turn reply WITHOUT an LLM call + cannot_answer/budget_exhausted
        escalation; the counter does NOT increment (no model call, no handoff).
      → MODERATION (§3.4.15): harmful inbound escalates to a human, no harmful output.
        Owner decision D2 (2026-09-14, round 7 WP-2 item 3): only threats_violence and
        self_harm take the conversation away (human_controlled); abuse_harassment and
        illegal_solicitation are recorded as content_flagged, the owner is told, and
        Luciel keeps answering.
      → ESCALATION GATE 1 — INTAKE (§3.4.5): explicit_human_request → acknowledge,
        hand off, skip the loop (the ONLY judgment signal that mutes Luciel, D2);
        strong_negative_sentiment → record + notify the owner, then answer the
        question and say the team has been told.
      → RETRIEVE (§3.2): knowledge retrieval; all retrieved content is wrapped as
        UNTRUSTED data before it ever reaches the model (§3.4.14 Defense).
      → [PLAN → ACT → REFLECT] × up to 5 (§3.4.1 iteration bound — cost control):
        the first model call fires the budget counter once (§3.4.1b). Tool calls
        dispatch via the Arc 12 broker (§3.3.3) using the five-outcome contract.
      → GROUNDING (§3.4.13): score the answer against retrieved chunks / tool result.
      → ESCALATION GATE 2 — OUTCOME (§3.4.5): BOTH signals are evaluated on every
        turn (round 7 WP-2, item 1). high_value_lead (an opportunity signal
        independent of answer quality) names the row when it fires; the
        answer-quality verdict — low confidence OR below the 0.50 grounding floor,
        with no resolving tool (harmony fix 2026-08-04 item 3: AND made this gate
        unsatisfiable in live mode because providers always report confidence=1.0)
        — rides on ``EscalationDecision.answer_ungrounded`` and replaces the draft
        with the honest hand-off whichever signal won the row. Alone, it is the
        cannot_answer signal.

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
is best-effort against the row and never blocks the lead's turn (§3.5.4): it starts as
a background task once the turn's transaction has committed (round 7 WP-2, item 5).

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
    schedule_notification_after_commit,
)
from app.observability.metrics import emit_metric
from app.runtime import budget, channel_switch, handoff, memory
from app.runtime import personality as personality_rules
from app.runtime.channel_arbiter import choose_outbound_channel
from app.runtime.escalation import (
    EscalationDecision,
    Gate,
    Signal,
    evaluate_intake,
    evaluate_outcome,
    is_contact_details_turn,
    is_conversational_turn,
    is_decline_turn,
    is_new_question,
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
from app.runtime.knowledge_retrieval import RetrieverBundle, is_retrieval_unavailable
from app.runtime.lead_capture import capture_or_update_lead
from app.runtime.llm_cap import LLMCapExceeded
from app.runtime.llm_router import LLMResponse, LLMRouter, ProviderError
from app.runtime.team_availability import follow_up_phrase
from app.runtime.team_availability import parse as parse_team_availability
from app.tools import contract as tool_contract
from app.tools.contract import Tool, ToolResult
from app.tools.schemas import (
    ACTION_TOOLS,
    HAND_TO_TEAM_TOOL_ID,
    build_tool_defs,
    with_cognition_tools,
)

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
# word "confirmed" ("orders are confirmed by email") is left alone. A PROMISE is not
# a claim either (round 6 dev walk 2026-09-13, R6-8): the operating-context stanza
# tells the model the platform delivers a copy elsewhere, so "I'll have a copy sent
# to your email once you confirm the address" is the honest wording of the channel
# switch, not a completed action - the gap between subject and verb refuses to cross
# a future/conditional modal. "I can confirm it has been sent" is still caught: the
# second subject ("it has been") starts a fresh match after the modal.
#
# The SUBJECT must be first person or the customer's own thing (round 7 WP-2, item
# 2): "I've booked", "we have sent", "your appointment has been scheduled", "it's
# been added to your calendar". A bare "has been / is now / are now" used to count as
# a subject, so a paraphrase of the knowledge base with a generic subject
# ("Appointments are now booked through the portal") was rewritten into the
# unfounded-claim replacement and escalated — a true statement about the business
# read as a lie about an action.
_FIRST_PERSON_SUCCESS_CLAIM = re.compile(
    r"\b(i'?ve|i have|we'?ve|we have|i|we"
    r"|it'?s been|it has been|that'?s been|that has been|this has been"
    r"|your [a-z][a-z' -]{0,30}?(?:has|have) been|your [a-z][a-z' -]{0,30}?(?:is|are) now)\b"
    r"(?:(?!\b(?:will|'ll|can|could|would|shall|should|may|might|going to|about to"
    r"|able to|happy to|glad to)\b)[^.!?]){0,30}?"
    r"\b(booked|scheduled|rescheduled|cancelled|canceled|sent|delivered"
    r"|added to (?:the|your) calendar|updated your)\b",
    re.IGNORECASE,
)


# A HAND-OFF claim (round 7 WP-2, item 2): the reply tells the customer the team has
# been (or will be) told — "I've flagged this for the team", "I'll pass this along",
# "someone will reach out", "your request has been forwarded". The model has exactly
# one way to make that true: the always-on ``hand_to_team`` tool. A reply that claims
# a hand-off on a turn with no recorded escalation used to ship with nothing behind
# it — no row, no owner notification, a customer waiting for a call that was never
# coming. The claim is not stripped: the customer was promised a person, so the
# orchestrator RECORDS the escalation through the standard path and the words become
# true. Offers ("I can pass this along if you'd like", "would you like me to flag
# this?") and statements about the business ("you can reach the team at …") are not
# claims and never match.
_HANDOFF_CLAIM = re.compile(
    # first-person, completed or promised: I've / I'll / we have / I will / I'm going to
    r"\b(?:i|we)(?:'ve|'ll|'m going to|'re going to| have| will| am going to| are going to)"
    r" (?:also |just |already |now |gone ahead and )?"
    r"(?:flag(?:ged)?|pass(?:ed)?(?: (?:this|that|it|your [a-z]+))? (?:along|on)"
    r"|let the (?:team|owner|manager) know|notif(?:y|ied)|escalat(?:e|ed)|forward(?:ed)?"
    r"|loop(?:ed)? in|rais(?:e|ed) (?:this|that|it)"
    r"|hand(?:ed)? (?:this|that|it|you) (?:over|off))\b"
    # a person will reach out: "someone will get back to you", "the team will be in touch"
    r"|\b(?:someone|somebody|a (?:team )?member|a person|the (?:team|owner|manager)|our team"
    r"|they)(?: from (?:the|our) team)?(?:'ll| will| is going to| are going to) "
    r"(?:reach out|get back|be in touch|follow up|contact you|call you|email you|text you)\b"
    # the customer's own thing, passive: "your request has been forwarded / flagged"
    r"|\b(?:your [a-z]+|this|that|it)(?: has| have|'s) been "
    r"(?:flagged|passed (?:along|on)|escalated|forwarded|shared with the team"
    r"|sent to the team|raised with the team)\b",
    re.IGNORECASE,
)


def _stalled_gather_reasons(reasoned: _Reasoned) -> tuple[str, ...]:
    """Why the §3.3.7 ask bound fired, in the owner's words (round 7 WP-2, item 3):
    the field names the broker kept refusing on, an ambiguity the customer never
    resolved, or a question the model asked twice in prose."""
    missing: list[str] = []
    ambiguous = False
    for _, result in reasoned.dispatched:
        if result.outcome is tool_contract.Outcome.missing_input:
            missing.extend(f for f in result.missing_fields if f not in missing)
        elif result.outcome is tool_contract.Outcome.ambiguous:
            ambiguous = True
    reasons: list[str] = []
    if missing:
        reasons.append(f"still missing {', '.join(missing)} after {ASK_ATTEMPT_BOUND} asks")
    if ambiguous:
        reasons.append("could not tell which one the customer meant")
    if not reasons:
        reasons.append(f"asked the same question {ASK_ATTEMPT_BOUND} times")
    return tuple(reasons)


def is_clarifying_question(text: str) -> bool:
    """A single short question with no assertion in it — the one ungrounded draft that
    is honest to send, because it claims nothing (F100)."""
    body = (text or "").strip()
    if not body.endswith("?") or len(body) > 160:
        return False
    return sum(body.count(ch) for ch in ".!?") == 1


# A question mark or an interrogative anywhere means the message SEEKS something,
# whatever else it is ("thanks — and do you open Saturdays?"): retrieval must run.
_CARRIES_QUESTION = re.compile(
    r"\?|\b(what|how|when|where|why|which|who|whether"
    r"|do you|can you|could you|would you|are you|is there|does it|did you)\b",
    re.IGNORECASE,
)


def retrieval_skippable(inbound_text: str, *, gather_pending: bool) -> bool:
    """True when this turn has nothing to look up (round 7 WP-2 item 3, cost).

    The retriever used to embed and search EVERY turn although the turn classifiers
    had already decided "thanks!" or "here's my email" carries no question. A social
    or contact-details turn with no question mark / interrogative, while no tool
    gather is pending, skips the embedding call and the Knowledge block. Gate 2 is
    untouched: it already exempts these turns from the floor, and a question in the
    same breath keeps both the search and the floor."""
    if gather_pending:
        return False
    if _CARRIES_QUESTION.search(inbound_text or ""):
        return False
    return is_conversational_turn(inbound_text) or is_contact_details_turn(inbound_text)


#: What the model is told about the knowledge search on a turn with NO chunks
#: (round 7 WP-2, item 4). Without these it could not tell "nothing matched" from
#: "the knowledge was never consulted": an absent block now means the search was
#: skipped (a social turn), and each of the other two states is said in so many
#: words. Both are deterministic platform notes, never a knowledge claim.
KNOWLEDGE_NONE_MATCHED = "The business's knowledge was searched and nothing matched this question."
KNOWLEDGE_UNAVAILABLE = "The knowledge could not be searched right now."

#: ``knowledge_state`` values ``_reason`` composes the prompt from.
_KNOWLEDGE_FOUND = "found"
_KNOWLEDGE_NONE_MATCHED = "none_matched"
_KNOWLEDGE_UNAVAILABLE = "unavailable"
_KNOWLEDGE_NOT_SEARCHED = "not_searched"
#: The key a platform-composed note carries on its message so a provider double
#: (EchoModel) can tell it from the customer's own words. Live providers ignore it.
PLATFORM_NOTE_KEY = "platform_note"


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

# Why no model served the turn (round 7 WP-2, item 4), for the llm_unavailable row.
# ``provider``: both providers failed (the pre-existing case, no reason on the row);
# ``cap``: the tenant's per-minute model-call cap (§9 C9) refused the turn — it used
# to escape as a bare exception, a JSON 429 to Twilio's voice webhook (a hang-up) and
# nothing at all to an SMS/WhatsApp sender.
_LLM_FAILURE_PROVIDER = "provider"
_LLM_FAILURE_CAP = "cap"
_LLM_FAILURE_REASONS: dict[str, tuple[str, ...]] = {
    _LLM_FAILURE_PROVIDER: (),
    _LLM_FAILURE_CAP: ("this account's per-minute model-call cap was reached",),
}

# Appended ONCE to the answer on the turn a strong_negative_sentiment row is written
# (round 7 WP-2, item 3; D2): the upset customer still gets their answer, and hears —
# truthfully, the row and notification exist — that a person is now aware.
_TEAM_TOLD_NOTE = " I've also let the team know, so a person can follow up with you."

# --- one hand-off ack, one contact ask, per session (round 7 WP-2 remainder, item 1) -- #
# Every below-floor turn used to replace the draft with the SAME full hand-off
# paragraph and append the SAME contact ask — three uncovered questions from an
# anonymous widget visitor read the identical block three times. The full ack and
# the ask now go out once; later turns get a short, varied acknowledgement, and the
# ask returns only after a respite, bounded like any other repeated ask (§3.3.7).
# All of it is read from the session's own transcript — no new persistence.

#: The code-composed contact ask for an anonymous escalated visitor (C5).
CONTACT_ASK_LINE = (
    "So the team can reach you, what's your name and the best email or phone number "
    "to contact you at?"
)
#: Luciel turns that must pass, with still no identifier on file, before an anonymous
#: visitor who did not answer the contact ask is asked again.
CONTACT_ASK_RESPITE_TURNS = 4
#: Short acknowledgements for a LATER question Luciel cannot answer, once the full
#: hand-off line has gone out this session. Each says only what is true — the team
#: already has the thread and this question is added to it — and promises nothing
#: twice. Varied by position so the visitor never reads the same line twice running.
SHORT_HANDOFF_ACKS: tuple[str, ...] = (
    "That's another one I can't answer from what I have — I've added it to what the "
    "team will follow up on.",
    "I don't have that on hand either; I've noted it for the team alongside your earlier question.",
    "I'm not able to confirm that myself, so I've passed it along with the rest for the team.",
)
# The full hand-off ack in any of its variants (_handoff_ack): "I've flagged this for
# the team" / "I've passed this along to the team", with or without a follow-up time.
_HANDOFF_ACK_MARK = re.compile(r"\b(?:flagged this for the team|passed this along to the team)\b")


def _luciel_turns(history: list[Transcript]) -> list[str]:
    return [t.text for t in history if t.role == "luciel"]


def _is_short_handoff_ack(text: str) -> bool:
    return any(text.startswith(ack) for ack in SHORT_HANDOFF_ACKS)


def handoff_ack_sent(history: list[Transcript]) -> bool:
    """True once this session's transcript carries the full hand-off ack."""
    return any(_HANDOFF_ACK_MARK.search(text) for text in _luciel_turns(history))


def short_handoff_ack(history: list[Transcript]) -> str:
    """The short acknowledgement for this turn — the next one in the cycle, so two
    consecutive uncovered questions never get the same sentence."""
    already = sum(1 for text in _luciel_turns(history) if _is_short_handoff_ack(text))
    return SHORT_HANDOFF_ACKS[already % len(SHORT_HANDOFF_ACKS)]


def contact_ask_due(history: list[Transcript], *, anonymous: bool) -> bool:
    """Whether the code-composed contact ask goes out on THIS turn.

    Only for an anonymous visitor (no lead on file, no transport identity, widget).
    The first escalated turn asks; after that the ask returns only once
    ``CONTACT_ASK_RESPITE_TURNS`` Luciel turns have passed with still nothing on
    file, and never more than ``ASK_ATTEMPT_BOUND`` times in a session — the
    code-composed re-ask counts against the same bound as the model's (§3.3.7).

    Never once the customer has declined a detail this session (item 2, rule 4):
    Luciel said "I won't ask for that again", and the contact ask names the very
    identifiers a booking gather asks for."""
    if not anonymous:
        return False
    turns = _luciel_turns(history)
    if any(DECLINED_INPUT_LINE in text for text in turns):
        return False
    asked_at = [i for i, text in enumerate(turns) if CONTACT_ASK_LINE in text]
    if not asked_at:
        return True
    if len(asked_at) >= ASK_ATTEMPT_BOUND:
        return False
    return len(turns) - asked_at[-1] - 1 >= CONTACT_ASK_RESPITE_TURNS


def unanswered_questions(history: list[Transcript]) -> list[str]:
    """The customer messages this session answered with a hand-off ack (full or
    short): the questions Luciel could not cover, in order."""
    out: list[str] = []
    last_lead: str | None = None
    for turn in history:
        if turn.role == "lead":
            last_lead = turn.text
        elif turn.role == "luciel" and last_lead is not None:
            if _HANDOFF_ACK_MARK.search(turn.text) or _is_short_handoff_ack(turn.text):
                out.append(last_lead)
                last_lead = None
    return out


# --- declining a required input (round 7 WP-2 remainder, item 2; §3.3.7 rule 4) ----- #
#: What a stalled tool was trying to do, in the customer's words — for the decline
#: reply ("I can't complete the booking without it") and the owner's reasons.
GATHER_WHAT: dict[str, str] = {
    "book_appointment": "the booking",
    "reschedule_appointment": "the change to your booking",
    "cancel_appointment": "the cancellation",
    "check_availability": "the availability check",
    "send_email": "sending that email",
    "send_sms": "sending that text",
    "schedule_callback": "the follow-up",
    "push_to_crm": "the update",
    "lookup_record": "the lookup",
}
#: Said once, in code, the moment a customer declines a detail: the ask stops here.
DECLINED_INPUT_LINE = "No problem — I won't ask for that again."
#: The decline reply when the conversation simply goes on (nothing was blocked on it).
DECLINED_INPUT_CONTINUE = f"{DECLINED_INPUT_LINE} Is there anything else I can help you with?"


@dataclass(frozen=True)
class PendingGather:
    """What the session's last stalled tool dispatch was waiting for: the field names
    the broker refused on, or an ambiguity the customer never resolved. Empty when
    the stall was a prose re-ask with no tool behind it (nothing is blocked)."""

    tool_id: str | None = None
    missing_fields: tuple[str, ...] = ()
    ambiguous: bool = False

    @property
    def blocks_action(self) -> bool:
        return bool(self.missing_fields) or self.ambiguous

    @property
    def what(self) -> str:
        return GATHER_WHAT.get(self.tool_id or "", "that")

    def field_reason(self) -> str:
        """The owner-facing reason naming what is still missing."""
        if self.missing_fields:
            return f"still missing {', '.join(self.missing_fields)}"
        return "could not tell which one the customer meant"


async def pending_gather(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> PendingGather:
    """Read the pending gather from the session's durable tool traces (§3.3.4 rule 3):
    the dispatches of the LATEST turn (those sharing the newest ``at``), a missing_input
    among them naming the fields, an ambiguous one the unresolved choice. Field NAMES
    only — the traces never carry the customer's values."""
    from app.models.trace import ToolTraceEvent  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(ToolTraceEvent)
                .where(ToolTraceEvent.admin_id == admin_id, ToolTraceEvent.session_id == session_id)
                .order_by(ToolTraceEvent.at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return PendingGather()
    latest = [r for r in rows if r.at == rows[0].at]
    missing: list[str] = []
    tool_id: str | None = None
    for row in latest:
        if row.outcome == tool_contract.Outcome.missing_input.value:
            tool_id = tool_id or row.tool_id
            missing.extend(f for f in (row.missing_fields or []) if f not in missing)
    if missing:
        return PendingGather(tool_id, tuple(missing))
    ambiguous = next(
        (r for r in latest if r.outcome == tool_contract.Outcome.ambiguous.value), None
    )
    if ambiguous is not None:
        return PendingGather(ambiguous.tool_id, (), True)
    return PendingGather(latest[0].tool_id)


def declined_input_reply(gather: PendingGather) -> str:
    """The reply when the customer declined a detail the action cannot go without: the
    ask stops, the limit is said plainly, and the hand-off is REAL (the row and the
    owner page are written before this is sent). No time is promised."""
    return (
        f"{DECLINED_INPUT_LINE} I can't complete {gather.what} without it myself, so I've "
        "passed this along to the team — a person can pick it up from here."
    )


async def record_or_extend_escalation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
    reasons: list[str],
) -> bool:
    """Write the signal's row for this session — or, when the session already carries
    one, add these reasons to it and page the owner AGAIN with them (round 7 WP-2
    remainder). The row stays one business event; the page carries the news. False
    when nothing new reached the row (every reason was already on it)."""
    if await _record_escalation(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate=gate,
        score=score,
        reasons=reasons,
    ):
        return True
    if not await _append_escalation_reasons(
        session,
        admin_id=admin_id,
        session_id=session_id,
        signal=signal,
        gate=gate,
        reasons=reasons,
    ):
        return False
    emit_metric("EscalationRenotified", dimensions={"Signal": signal})
    schedule_notification_after_commit(
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


#: Longest excerpt of a new unanswered question carried on the row's reasons.
_QUESTION_EXCERPT_CAP = 120


def also_asked_reason(question: str) -> str:
    """The owner-facing reason for a re-notification: the new question itself."""
    excerpt = " ".join((question or "").split())
    if len(excerpt) > _QUESTION_EXCERPT_CAP:
        excerpt = excerpt[: _QUESTION_EXCERPT_CAP - 1].rstrip() + "…"
    return f'also asked: "{excerpt}"'


#: Moderation categories that do NOT take the conversation away (D2): recorded under
#: content_flagged with the owner-facing reason, the owner is notified, and the loop
#: runs — the MODERATION stanza keeps the model professional. Threats and self-harm
#: are absent on purpose: they keep the take-over path.
_MODERATION_KEEP_ANSWERING: frozenset[ModerationCategory] = frozenset(
    {ModerationCategory.abuse_harassment, ModerationCategory.illegal_solicitation}
)

#: The owner-facing phrase for each moderation category (round 7 WP-2, item 7).
_MODERATION_REASON: dict[ModerationCategory, str] = {
    ModerationCategory.threats_violence: "threatened violence",
    ModerationCategory.self_harm: "said they may harm themselves",
    ModerationCategory.abuse_harassment: "abusive language toward the assistant or business",
    ModerationCategory.illegal_solicitation: "asked for something illegal",
}


def moderation_reasons(category: ModerationCategory) -> tuple[str, ...]:
    """The reasons every moderation escalation row carries (round 7 WP-2, item 7):
    FIRST a stable ``moderation:<category>`` entry the delivery layer routes and
    labels on, then the owner-facing phrase. Threats/self-harm rows (which take the
    conversation away under explicit_human_request, D2) used to carry no reasons at
    all, so the owner could not tell a threat from a polite request for a person."""
    return (f"moderation:{category.value}", _MODERATION_REASON[category])


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


async def _append_escalation_reasons(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    session_id: uuid.UUID,
    signal: str,
    gate: str,
    reasons: list[str],
) -> bool:
    """Add owner-facing reasons to the session's EXISTING row for this signal/gate.

    A signal fires once per session, so a later, more specific reason for the same
    signal (round 7 WP-2, item 3: the §3.3.7 ask bound naming the fields still
    missing, after an earlier below-floor turn already wrote the cannot_answer row)
    has nowhere else to live. Distinct phrases are appended; nothing is re-paged.
    Returns whether the row changed."""
    row = (
        await session.execute(
            select(EscalationEvent).where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.session_id == session_id,
                EscalationEvent.signal == signal,
                EscalationEvent.gate == gate,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    existing = [str(r) for r in (row.reasons or [])]
    merged = [*existing, *(r for r in reasons if r not in existing)]
    if merged == existing:
        return False
    row.reasons = merged
    await session.flush()
    return True


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
    audit_detail: dict[str, object] | None = None,
) -> bool:
    """Write the escalation_events row + the audit event, then notify (§3.5.2 / §5.2).

    The row is written and flushed FIRST: it is the authoritative record and delivery is
    best-effort against it (§3.5.2). Notification never blocks or breaks the lead's turn
    (§3.5.4): it is scheduled to start after this transaction commits, as a background
    task in its own tenant session (item 5) — a test that asserts delivery drains it
    with ``escalation_dispatch.drain_background_notifications()``.

    ONCE per (session, signal, gate): the insert itself is the arbiter (unique index
    ``uq_escalation_once``, 2026-09-05 audit F053/F061) — a pre-check-then-insert let
    two turns racing on the same session page the owner twice. Returns whether this
    call wrote the row; a loser writes nothing and notifies nobody.

    ``audit_detail`` rides on the ``escalation_fired`` audit payload only (round 7
    WP-2, item 7: the moderation category) — non-identifying keys, never lead PII."""
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
        # No raw lead PII in the payload (§3.4.11) — signal + session id (+ the
        # moderation category when one applies), never the customer's words.
        payload={
            "signal": signal,
            "gate": gate,
            "session_id": str(session_id),
            **(audit_detail or {}),
        },
    )
    await session.flush()
    # Delivery starts AFTER this transaction commits, off the customer's turn (§3.5.4;
    # round 7 WP-2 item 5) — it used to be awaited right here, before the reply was
    # composed, with blocking SDK calls on the event loop.
    schedule_notification_after_commit(
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
            # The always-on hand_to_team cognition tool (round 7 WP-2, item 2) is
            # described on EVERY turn, tools wired or not — the model must be able to
            # reach the team on any turn, or it will promise a hand-off it cannot make.
            tool_catalog=with_cognition_tools(None),
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
        # because capture only ran after the gate. It also runs BEFORE the
        # human-controlled early return below (round 7 WP-2, item 8): a visitor who
        # types their email while a person holds the session used to have it dropped
        # on the floor, because the take-over return sat above this block.
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

        # Human-controlled sessions STOP the loop (§3.4.12): the inbound is persisted
        # for the dashboard live feed and the lead record was maintained above (item
        # 8); no LLM call is made and nothing is said.
        if not handoff.loop_should_run(mode):
            return LoopResult(
                reply_text="",
                outbound_channel=channel,
                counted=False,
                handed_off=True,
            )

        # The session's own transcript, loaded once for every path below: the intake
        # gate reads the trailing lead turns from it, and the hand-off ack / contact
        # ask decide from it whether they already went out this session (round 7
        # WP-2 remainder, item 1) — the at-cap and moderation replies included.
        history = await memory.load_session_history(
            self.session, admin_id=admin_id, session_id=session_id
        )

        # --- CONVERSATION BUDGET GATE (§3.4.1b) — before PLAN ----------------- #
        budget_state = await budget.check_budget(
            self.session,
            admin_id=admin_id,
            instance_id=instance_id,
            has_payment_method=self.has_payment_method,
            # Legal §A3 #3: a conversation already counted is served to its end; the
            # cap gates NEW conversations (round 7 WP-1 #1).
            session_id=session_id,
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
                history=history,
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

        # An escalation recorded at INTAKE that leaves the conversation with Luciel
        # (D2, round 7 WP-2 item 3): the row + owner notification are written here,
        # the loop runs, and the turn reports it. ``team_told`` appends the one-line
        # note to the answer on the turn the sentiment row was actually written.
        standing: EscalationDecision | None = None
        team_told = False

        # --- CONTENT MODERATION (§3.4.15) ------------------------------------ #
        mod = moderate(inbound_text)
        if mod.should_escalate:
            await self._audit_moderation(session_id, mod.category.value)
            # Every moderation row: "moderation:<category>" first, then the owner's
            # phrase (item 7); the category also reaches the escalation_fired audit.
            reasons = moderation_reasons(mod.category)
            audit_detail: dict[str, object] = {"moderation_category": mod.category.value}
            if mod.category in _MODERATION_KEEP_ANSWERING:
                # D2: abuse / an illegal ask are told to the owner; Luciel stays
                # professional (MODERATION stanza) and keeps answering — no take-over.
                await _record_escalation(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=Signal.content_flagged.value,
                    gate=Gate.intake.value,
                    score=None,
                    reasons=list(reasons),
                    audit_detail=audit_detail,
                )
                standing = EscalationDecision(
                    True, Signal.content_flagged, Gate.intake, None, reasons
                )
            else:
                # Threats and self-harm: a person takes the conversation (D2).
                if mod.category is ModerationCategory.self_harm:
                    # Immediate escalation with crisis copy; no contact ask (F116).
                    ack = SELF_HARM_REPLY
                else:
                    ack = await self._handoff_reply(
                        history,
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
                    reasons=reasons,
                    audit_detail=audit_detail,
                )

        # Monitoring-only injection signal (NOT a security boundary, §3.4.14).
        if detect_injection(inbound_text):
            await self._audit_injection(session_id)

        # --- ESCALATION GATE 1 — INTAKE (§3.4.5) ----------------------------- #
        trailing = [t.text for t in history if t.role == "lead"]
        intake = evaluate_intake(trailing)
        if intake.fired and intake.signal is Signal.explicit_human_request:
            # The ONE judgment signal that takes the conversation away (D2): the
            # customer asked for a person, so a person answers from here on.
            return await self._hand_off_with_ack(
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                channel=channel,
                signal=Signal.explicit_human_request,
                gate="intake",
                score=intake.score_or_confidence,
                reasons=intake.reasons,
                ack=await self._handoff_reply(
                    history,
                    channel=channel,
                    resolved_lead_id=resolved_lead_id,
                    transport_identifier=transport_identifier,
                ),
            )
        if intake.fired:
            # strong_negative_sentiment (D2): the owner is told, the customer still
            # gets their question answered by Luciel, and — on the turn the row is
            # written — hears that a person is now aware. Once per session.
            standing_signal = intake.signal or Signal.strong_negative_sentiment
            team_told = await _record_escalation(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                signal=standing_signal.value,
                gate=Gate.intake.value,
                score=intake.score_or_confidence,
                reasons=list(intake.reasons),
            )
            standing = standing if standing is not None else intake

        # --- RETRIEVE (§3.2) — wrap as UNTRUSTED before the model (§3.4.14) --- #
        # Harmony wave 2, item 2a: real chunk attribution for persistence, kept
        # separate from `raw_chunks` (which only ever feeds the model/scorer).
        # None when this construction site has no attribution source bound (voice,
        # bare fakes in tests) -- persisted as attribution_unavailable, never
        # fabricated. When bound, an empty list here is a REAL "retrieval found
        # nothing", distinct from None.
        attributed_chunks: list[dict[str, object]] | None = None
        gather_pending = sess_obj is not None and sess_obj.input_ask_attempts > 0

        # --- DECLINED INPUT (§3.3.7 rule 4; round 7 WP-2 remainder, item 2) --- #
        # The last turn stalled asking for a detail and the customer now declines
        # to give it: the ask stops HERE, in code — never a re-ask in fresh words.
        # A tool that cannot run without it makes this a real hand-off (row, owner
        # page naming the field, the honest line); a prose stall just moves on.
        declined_prefix = ""
        if sess_obj is not None and gather_pending and is_decline_turn(inbound_text):
            sess_obj.input_ask_attempts = 0
            gather_pending = False
            gather = await pending_gather(self.session, admin_id=admin_id, session_id=session_id)
            if gather.blocks_action:
                emit_metric("GatherDeclined", dimensions={"Channel": channel})
                decline_reasons = [
                    f"customer declined to give {', '.join(gather.missing_fields)}"
                    if gather.missing_fields
                    else "customer declined to say which one they meant",
                    f"{gather.what} cannot go ahead without it",
                ]
                await record_or_extend_escalation(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=Signal.cannot_answer.value,
                    gate=Gate.outcome.value,
                    score=None,
                    reasons=decline_reasons,
                )
                declined_text = declined_input_reply(gather)
                declined_row = await persist_turn(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    role="luciel",
                    text=declined_text,
                    at=datetime.now(UTC),
                    deterministic=True,
                )
                return LoopResult(
                    reply_message_id=declined_row.id,
                    reply_text=declined_text,
                    outbound_channel=channel,
                    counted=False,
                    escalation=EscalationDecision(
                        True, Signal.cannot_answer, Gate.outcome, None, tuple(decline_reasons)
                    ),
                )
            if not _CARRIES_QUESTION.search(inbound_text):
                # Nothing was blocked and nothing was asked: acknowledge and move on,
                # with no model call to tempt a re-ask.
                declined_row = await persist_turn(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    role="luciel",
                    text=DECLINED_INPUT_CONTINUE,
                    at=datetime.now(UTC),
                    deterministic=True,
                )
                return LoopResult(
                    reply_message_id=declined_row.id,
                    reply_text=DECLINED_INPUT_CONTINUE,
                    outbound_channel=channel,
                    counted=False,
                    escalation=standing,
                )
            # The decline came with a question: the loop answers it, and the reply
            # opens by taking no for an answer.
            declined_prefix = f"{DECLINED_INPUT_LINE} "

        if retrieval_skippable(inbound_text, gather_pending=gather_pending):
            # Nothing to look up (item 3): no embedding call, no Knowledge block, and
            # the row carries no attribution because no search ran (never a false
            # "searched, nothing matched").
            raw_chunks: list[str] = []
            knowledge_state = _KNOWLEDGE_NOT_SEARCHED
        else:
            if self.retriever_bundle is not None:
                raw_chunks, attributed_chunks = await self.retriever_bundle(inbound_text)
            else:
                raw_chunks = await self.retriever(inbound_text)
                if self.chunk_attributor is not None:
                    attributed_chunks = await self.chunk_attributor(inbound_text)
            if is_retrieval_unavailable(attributed_chunks):
                knowledge_state = _KNOWLEDGE_UNAVAILABLE
            elif raw_chunks:
                knowledge_state = _KNOWLEDGE_FOUND
            else:
                knowledge_state = _KNOWLEDGE_NONE_MATCHED
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
        # --- REQUIRED INPUTS ALREADY ON FILE (round 7 WP-2 remainder, item 10) --- #
        # The lead's CONFIRMED email/phone — or the transport identifier itself —
        # fill a required input the model leaves out, at the broker; the stanza names
        # the fields so the model stops asking for what the customer already gave.
        gathered: dict[str, Any] = (
            await channel_switch.confirmed_inputs_on_file(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
            )
            if tool_defs
            else {}
        )
        system_prompt = (
            build_system_prompt(
                self.business_context,
                self.luciel_email_address,
                self.personality,
                assistant_name=self.assistant_name,
                tool_catalog=tool_defs,
                operating_context=self.operating_context,
                on_file=sorted(gathered),
            )
            if tool_defs
            else self.system_prompt
        )

        # --- PLAN → ACT → REFLECT × up to 5 (§3.4.1) ------------------------- #
        resp, iterations, used_fallback, llm_failure = await self._reason(
            inbound_text,
            history,
            wrapped_chunks,
            resolved_lead_id=resolved_lead_id,
            prior_summaries=prior_summaries,
            lead_context=lead_context,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            session_id=session_id,
            knowledge_state=knowledge_state,
            gathered=gathered,
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
        if llm_failure is not None:
            # No model served the turn — both providers down, or the tenant's cap
            # refused it (item 4) → llm_unavailable escalation (§3.4.5 server signal)
            # with the SAME graceful, un-billed, deterministic reply on every channel
            # (voice stays TwiML). The cap case is distinct in the metric and on the
            # row's reasons, never a different customer experience.
            failure_reasons = _LLM_FAILURE_REASONS[llm_failure]
            if llm_failure == _LLM_FAILURE_CAP:
                _LOG.warning("llm_cap_exceeded session=%s channel=%s", session_id, channel)
                emit_metric("LLMCapExceeded", dimensions={"Channel": channel})
            await self._emit_llm_unavailable(session_id, resolved_lead_id, reasons=failure_reasons)
            ack = self._with_contact_ask(
                "I'm having a brief technical issue — I've flagged this so a person "
                "can follow up with you shortly.",
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
                history=history,
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
                escalation=EscalationDecision(
                    True, Signal.llm_unavailable, Gate.outcome, None, failure_reasons
                ),
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
                    # D2 (round 7 WP-2, item 3): stop asking, tell the owner WHAT is
                    # still missing, send the honest hand-off line instead of the
                    # re-ask — and stay in ai mode. The next turn is answered.
                    sess_obj.input_ask_attempts = 0
                    stall_reasons = _stalled_gather_reasons(resp)
                    written = await _record_escalation(
                        self.session,
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        resolved_lead_id=resolved_lead_id,
                        signal=Signal.cannot_answer.value,
                        gate=Gate.outcome.value,
                        score=None,
                        reasons=list(stall_reasons),
                    )
                    if not written:
                        # The session's one cannot_answer row already exists (an
                        # earlier below-floor turn): the owner still reads WHICH
                        # fields the gather stalled on.
                        await _append_escalation_reasons(
                            self.session,
                            admin_id=admin_id,
                            session_id=session_id,
                            signal=Signal.cannot_answer.value,
                            gate=Gate.outcome.value,
                            reasons=list(stall_reasons),
                        )
                    ack = await self._handoff_reply(
                        history,
                        channel=channel,
                        resolved_lead_id=resolved_lead_id,
                        transport_identifier=transport_identifier,
                    )
                    ack_row = await persist_turn(
                        self.session,
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        role="luciel",
                        text=ack,
                        at=datetime.now(UTC),
                        deterministic=True,
                    )
                    return LoopResult(
                        reply_message_id=ack_row.id,
                        reply_text=ack,
                        outbound_channel=channel,
                        counted=counted,
                        escalation=EscalationDecision(
                            True, Signal.cannot_answer, Gate.outcome, None, stall_reasons
                        ),
                        used_fallback=used_fallback,
                        iterations=iterations,
                        tool_outcomes=resp.tool_outcomes,
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
            row_written = False
            if not suppress:
                # ONE row for the turn: the signal that won it, with BOTH sets of
                # reasons when a hot lead also got an ungrounded draft (round 7
                # WP-2, item 1) — the owner reads "named a budget; answer only 10%
                # backed by your knowledge" on the same line.
                row_written = await _record_escalation(
                    self.session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=fired_signal,
                    gate="outcome",
                    score=outcome.score_or_confidence,
                    reasons=list(outcome.reasons),
                )
            elif outcome.answer_ungrounded and outcome.signal is not Signal.cannot_answer:
                # The lead signal already fired earlier in this session, but THIS
                # turn's answer is a guess: that is cannot_answer news of its own
                # (once per session like any other signal), never buried under the
                # suppressed repeat.
                if not await _session_already_escalated(
                    self.session,
                    admin_id=admin_id,
                    session_id=session_id,
                    signal=Signal.cannot_answer.value,
                ):
                    row_written = await _record_escalation(
                        self.session,
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        resolved_lead_id=resolved_lead_id,
                        signal=Signal.cannot_answer.value,
                        gate="outcome",
                        score=min(resp.confidence, grounding.score),
                        reasons=list(outcome.answer_reasons),
                    )
            if outcome.answer_ungrounded and not row_written:
                # The session's row already exists and THIS question is a new one
                # Luciel cannot cover (round 7 WP-2 remainder, item 1): the owner
                # reads it on the row and is paged again — a repeat pages nobody.
                await self._renotify_new_question(
                    history,
                    inbound_text=inbound_text,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    fired_signal=fired_signal,
                )

        # --- LUCIEL'S OWN HAND-OFF (round 7 WP-2, item 2; Vision §3.4) ------- #
        # The model called hand_to_team this turn: the row + notification were
        # written at dispatch (so the tool result it read back was true). The turn
        # reports it as its escalation when no platform gate named one first; the
        # session stays in ai mode (D2) — Luciel keeps answering.
        handed_to_team = any(
            str(call.get("tool_id", "")) == HAND_TO_TEAM_TOOL_ID
            and r.outcome is tool_contract.Outcome.ok
            for call, r in resp.dispatched
        )
        if escalation is None and handed_to_team:
            escalation = EscalationDecision(
                True,
                Signal.luciel_requested_handoff,
                Gate.outcome,
                None,
                ("Luciel asked for a person",),
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
                escalation = EscalationDecision(
                    True, Signal.cannot_answer, None, None, answer_ungrounded=True
                )
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
        if not claimed_without_action and escalation is None and _HANDOFF_CLAIM.search(reply_text):
            # The reply promises the team a hand-off that nothing recorded (round 7
            # WP-2, item 2): no gate fired, hand_to_team was not called. The customer
            # was promised a person, so the promise is made REAL through the same path
            # the tool uses — row, audit, owner notification — and the words are left
            # as they are, now honest. (Once per session: a row already written by an
            # earlier hand_to_team call backs this claim too, and pages nobody twice.)
            emit_metric("HandoffClaimedWithoutTool", dimensions={"Channel": channel})
            promised = ("Luciel told the customer the team would follow up",)
            await _record_escalation(
                self.session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                signal=Signal.luciel_requested_handoff.value,
                gate=Gate.outcome.value,
                score=None,
                reasons=list(promised),
            )
            escalation = EscalationDecision(
                True, Signal.luciel_requested_handoff, Gate.outcome, None, promised
            )
        if (
            not claimed_without_action
            and escalation is not None
            and escalation.answer_ungrounded
            and not is_clarifying_question(reply_text)
        ):
            # Below the grounding floor the draft is a guess (2026-09-05 audit F100):
            # it used to ship anyway with "I've flagged this" appended, so the
            # visitor got a confident wrong answer AND a hand-off. An employee who
            # does not know says so and gets a person — the draft never leaves. A
            # bare clarifying question ("what's your email?") asserts nothing and
            # still goes out; an unfounded action claim keeps its own, more
            # specific replacement above. The verdict is read from
            # ``answer_ungrounded``, not from the signal (round 7 WP-2, item 1): a
            # hot lead whose row says high_value_lead gets the same honest ack, not
            # the fabricated draft that used to ship under the winning signal. The
            # FULL ack once per session; a later uncovered question gets a short,
            # varied acknowledgement instead of the same paragraph again (remainder
            # item 1).
            reply_text = (
                short_handoff_ack(history)
                if handoff_ack_sent(history)
                else await self._handoff_ack()
            )
            deterministic_reply = True
        if declined_prefix:
            # The customer declined a detail and asked something in the same breath
            # (item 2): whatever answers the question — the model's grounded reply
            # or the honest hand-off line for an uncovered one — opens by taking no
            # for an answer.
            reply_text = declined_prefix + reply_text
        if team_told and not deterministic_reply:
            # The sentiment row was written THIS turn (D2): the answer stands, and the
            # customer hears that a person is aware — a canned hand-off line already
            # says so, so it is never doubled.
            reply_text = reply_text.rstrip() + _TEAM_TOLD_NOTE
        if escalation is None:
            escalation = standing
        # The customer asked for this answer on another channel (round 6 WP-H, F060).
        # Decided BEFORE the contact ask: when the switch below will itself ask for an
        # address, the escalation-time contact ask is not doubled on top of it.
        requested_channel = channel_switch.detect_requested_channel(inbound_text)
        switch_will_ask = (
            requested_channel is not None
            and requested_channel != channel
            and requested_channel in channel_switch.KIND_FOR_CHANNEL
            and requested_channel in (self.enabled_channels or {channel})
            and sess_obj is not None
        )
        # Escalation-time contact ask — shared helper since C5; see _with_contact_ask.
        # Never on the turn the customer declined a detail (item 2): "I won't ask for
        # that again" followed by "what's the best email" is the re-ask rule 4 forbids.
        if escalation is not None and not switch_will_ask and not declined_prefix:
            reply_text = self._with_contact_ask(
                reply_text,
                channel=channel,
                resolved_lead_id=resolved_lead_id,
                transport_identifier=transport_identifier,
                history=history,
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
            customer_requested_channel=requested_channel,
            # Any escalation this turn recorded — a gate's, the model's own hand_to_team,
            # a promised hand-off made real — is the urgent case the arbiter weighs.
            is_urgent_escalation=escalation is not None,
        )
        if outbound != channel and outbound in channel_switch.KIND_FOR_CHANNEL:
            if requested_channel == outbound and sess_obj is not None:
                # The customer ASKED for it there (round 6 WP-H, F060): a confirmed
                # address gets it now; otherwise ask where and park the answer. This
                # runs whether or not the turn escalated (round 7 WP-2, item 6):
                # "email me your prices, I'd like to book this week" is a hot lead AND
                # a request for an address — it used to be neither asked nor sent.
                reply_text = await self._switch_or_ask(
                    sess_obj,
                    outbound=outbound,
                    reply_text=reply_text,
                    inbound_text=inbound_text,
                    resolved_lead_id=resolved_lead_id,
                )
            elif escalation is None and outbound in _CHANNEL_OFFER:
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
    async def _turn_tool_defs(self) -> list[dict[str, Any]]:
        """The provider-neutral tool defs THIS turn may propose (C4).

        Intersection of: the session's frozen snapshot capability set (§3.8.7 rule E;
        None = older session → no snapshot filter), the instance's LIVE enabled
        authorizations (default-deny — an unauthorized tool is never even described
        to the model), and the ids the resolver can bind — PLUS the always-on
        ``hand_to_team`` cognition tool (round 7 WP-2, item 2), which no gate governs:
        it is Luciel's own judgment, dispatched by the loop itself, never listed on the
        Configure surface, and present on every turn whether or not tools are wired."""
        return with_cognition_tools(await self._catalog_tool_defs())

    async def _catalog_tool_defs(self) -> list[dict[str, Any]] | None:
        """The gated CATALOG defs alone (see ``_turn_tool_defs``); None when none apply."""
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
        knowledge_state: str = _KNOWLEDGE_NOT_SEARCHED,
        gathered: dict[str, Any] | None = None,
    ) -> tuple[_Reasoned, int, bool, str | None]:
        """PLAN→ACT→REFLECT up to MAX_ITERATIONS (§3.4.1). Returns the final reasoned
        answer, the iteration count, whether the fallback provider was used, and why
        no model served the turn: None, ``_LLM_FAILURE_PROVIDER`` (BOTH providers
        failed) or ``_LLM_FAILURE_CAP`` (the tenant's per-minute cap refused it).

        ``knowledge_state`` (item 4) says what the Knowledge block's absence means:
        chunks found → the block; searched and nothing matched, or the search could
        not run → the matching deterministic note; not searched → nothing at all.

        The cap is admitted ONCE at the turn boundary (``router.turn()``, round 7
        WP-2 item 4): the iterations inside share that admission, so a tool-using
        turn is one of the fifty, not five.

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
        elif knowledge_state == _KNOWLEDGE_NONE_MATCHED:
            # Searched, nothing matched (item 4): said in so many words, so the model
            # answers from what it was given — or says it does not know — instead of
            # reading an absent block as "I was never told anything about this".
            messages.append(
                {"role": "user", "content": KNOWLEDGE_NONE_MATCHED, PLATFORM_NOTE_KEY: True}
            )
        elif knowledge_state == _KNOWLEDGE_UNAVAILABLE:
            # The embedding provider was down (the bundle persisted its marker): a
            # different truth from "nothing matched", and the model is told which.
            messages.append(
                {"role": "user", "content": KNOWLEDGE_UNAVAILABLE, PLATFORM_NOTE_KEY: True}
            )

        prompt = system_prompt if system_prompt is not None else self.system_prompt
        used_fallback = False
        tool_outcomes: list[str] = []
        dispatched: list[tuple[dict[str, Any], ToolResult]] = []
        last: LLMResponse | None = None
        iterations = 0
        try:
            # ONE cap admission for the whole turn (item 4): raises LLMCapExceeded at
            # entry when the tenant is over its per-minute cap — no model is called.
            with self.router.turn():
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
                        # Router already failed primary→fallback; a ProviderError here
                        # means BOTH providers are down (§3.4.3 immediate failover).
                        return (
                            _Reasoned("", 0.0, tool_outcomes, dispatched),
                            iterations,
                            used_fallback,
                            _LLM_FAILURE_PROVIDER,
                        )
                    used_fallback = used_fallback or resp.used_fallback
                    last = resp
                    if not resp.tool_calls:
                        # Natural completion: the model answered without (further)
                        # tool use.
                        break
                    # ACT: dispatch the proposed calls via the Arc 12 broker contract,
                    # then REFLECT: append the assistant's tool turn + one structured
                    # tool_result turn per call (untrusted-wrapped — results embed
                    # lead/provider content).
                    results = await self._dispatch_tools(
                        resp.tool_calls,
                        resolved_lead_id=resolved_lead_id,
                        session_id=session_id,
                        gathered=gathered,
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
        except LLMCapExceeded:
            # The tenant's cap refused the turn (§9 C9): the same honest, un-billed
            # reply path as a provider outage, under its own reason — never a bare
            # exception to the transport (a JSON 429 hangs up a Twilio call).
            return (
                _Reasoned("", 0.0, tool_outcomes, dispatched),
                iterations,
                used_fallback,
                _LLM_FAILURE_CAP,
            )

        if last is None:
            # Unreachable: MAX_ITERATIONS >= 1 so the loop always assigns or returns.
            raise RuntimeError("reasoning produced no response")
        return (
            _Reasoned(last.text, last.confidence, tool_outcomes, dispatched),
            iterations,
            used_fallback,
            None,
        )

    async def _dispatch_tools(
        self,
        tool_calls: list[dict[str, object]],
        *,
        resolved_lead_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        gathered: dict[str, Any] | None = None,
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
        # The business's clock (round 7 WP-2 remainder, item 11): the scheduling tools
        # read an offset-less time in it and render every time they return in it — the
        # same zone the operating-context stanza told the model about this turn.
        timezone = self.operating_context.timezone if self.operating_context else None
        results: list[tuple[dict[str, Any], ToolResult]] = []
        for call in tool_calls:
            tool_id = str(call.get("tool_id", ""))
            inputs = call.get("inputs", {})
            if tool_id == HAND_TO_TEAM_TOOL_ID:
                # Always-on cognition (round 7 WP-2, item 2): no snapshot, no
                # authorization row, no connection — Luciel may always reach the team.
                results.append(
                    (
                        dict(call),
                        await self._hand_to_team(
                            dict(inputs) if isinstance(inputs, dict) else {},
                            session_id=session_id,
                            resolved_lead_id=resolved_lead_id,
                        ),
                    )
                )
                continue
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
                gathered=gathered,  # item 10: confirmed values fill a missing input
                timezone=timezone,  # item 11: the business's clock for the tools
            )
            results.append((dict(call), result))
        return results

    #: Longest reason the model may attach to a hand-off (owner-facing phrase, F124).
    HAND_TO_TEAM_REASON_CAP = 240

    async def _hand_to_team(
        self,
        inputs: dict[str, Any],
        *,
        session_id: uuid.UUID | None,
        resolved_lead_id: uuid.UUID | None,
    ) -> ToolResult:
        """Execute ``hand_to_team`` (round 7 WP-2, item 2): Luciel's own judgment that a
        person should step in, recorded through the STANDARD escalation path — the
        ``escalation_events`` row, the ``escalation_fired`` audit event, the owner
        notification carrying the model's reason. The session is NOT taken away (D2):
        Luciel keeps answering while the team is on their way.

        Once per conversation like every other signal: a second call in the same
        session writes nothing and pages nobody, and says so in its payload — the
        team IS flagged, so the outcome is still ``ok``. A blank reason is recorded as
        such rather than refused: an escalation the model asked for is never dropped
        on a formality, and the owner reads "no reason given" instead of nothing."""
        if session_id is None:
            return tool_contract.failed("no_session")
        reason = " ".join(str(inputs.get("reason") or "").split())[: self.HAND_TO_TEAM_REASON_CAP]
        reasons = ["Luciel asked for a person", reason or "no reason given"]
        written = await _record_escalation(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=Signal.luciel_requested_handoff.value,
            gate=Gate.outcome.value,
            score=None,
            reasons=reasons,
        )
        return tool_contract.ok(
            {
                "passed_to_team": True,
                "state": "recorded" if written else "already_passed_this_conversation",
                # Read back by the model (T3): the honest wording of what happens next.
                "note": (
                    "The team has been notified and a person will follow up later; keep "
                    "helping the customer now. Never promise a time or a real-time reply."
                ),
            }
        )

    async def _renotify_new_question(
        self,
        history: list[Transcript],
        *,
        inbound_text: str,
        session_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None,
        fired_signal: str,
    ) -> bool:
        """A DISTINCT question Luciel could not answer after the session's row already
        exists (round 7 WP-2 remainder, item 1).

        A signal's row is written once per session, so a second uncovered question
        used to vanish: the owner was paged about the first and never heard that the
        visitor went on to ask two more things nobody could answer. Now the new
        question rides on the row's reasons (``also asked: "…"``) and the SAME row is
        notified again — the row stays one business event, the page carries the
        news. A repeat of an earlier uncovered question ("implants??" after "do you
        do implants?") is judged by ``is_new_question`` and pages nobody. Returns
        whether a re-notification was scheduled."""
        if not is_new_question(inbound_text, unanswered_questions(history)):
            return False
        signal = (
            Signal.cannot_answer.value
            if await _session_already_escalated(
                self.session,
                admin_id=self.admin_id,
                session_id=session_id,
                signal=Signal.cannot_answer.value,
            )
            else fired_signal
        )
        # The row exists (the caller checked), so this extends it and re-pages.
        return await record_or_extend_escalation(
            self.session,
            admin_id=self.admin_id,
            instance_id=self.instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=Gate.outcome.value,
            score=None,
            reasons=[also_asked_reason(inbound_text)],
        )

    @staticmethod
    def _with_contact_ask(
        reply_text: str,
        *,
        channel: str,
        resolved_lead_id: uuid.UUID | None,
        transport_identifier: str | None,
        history: list[Transcript],
    ) -> str:
        """Append the contact ask for an anonymous escalated visitor (C5).

        Owner-directed 2026-08-18, extended audit round 3: when a turn escalates on a
        channel that carries NO transport identity and no lead is on file, the team
        has been flagged about someone they cannot reach — so ask, deterministically,
        in code. Previously only the gate-2 path asked; gate-1 (the most likely
        trigger: "can I talk to a human"), moderation, at-cap, and llm_unavailable
        all escalated an anonymous widget visitor without asking. Strong channels
        (voice/SMS/email/Meta) skip this: the transport already names the person.

        Once per session, then a bounded, respited re-ask (round 7 WP-2 remainder,
        item 1; see ``contact_ask_due``): every escalating turn used to nag."""
        anonymous = (
            resolved_lead_id is None and transport_identifier is None and channel == "widget"
        )
        if contact_ask_due(history, anonymous=anonymous):
            return f"{reply_text}\n\n{CONTACT_ASK_LINE}"
        return reply_text

    async def _handoff_reply(
        self,
        history: list[Transcript],
        *,
        channel: str,
        resolved_lead_id: uuid.UUID | None,
        transport_identifier: str | None,
    ) -> str:
        """The hand-off line for THIS turn (round 7 WP-2 remainder, item 1): the full
        ack the first time this session, a short varied acknowledgement after — the
        team already has the thread — with the contact ask only when it is due."""
        ack = short_handoff_ack(history) if handoff_ack_sent(history) else await self._handoff_ack()
        return self._with_contact_ask(
            ack,
            channel=channel,
            resolved_lead_id=resolved_lead_id,
            transport_identifier=transport_identifier,
            history=history,
        )

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
        audit_detail: dict[str, object] | None = None,
    ) -> LoopResult:
        """Acknowledge to the customer, record the escalation, and set the session
        human_controlled (§3.4.12 Luciel-initiated handoff). The session counts as
        one budget unit (§3.4.1b).

        Owner decision D2 (2026-09-14): ONLY an explicit request for a person and the
        threats_violence / self_harm moderation categories come through here. Strong
        negative sentiment, the ask bound, abuse and an illegal ask record + notify
        and leave the conversation with Luciel (see run_turn)."""
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
            audit_detail=audit_detail,
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
            escalation=EscalationDecision(True, signal, None, score, reasons),
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
        self,
        session_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None,
        *,
        reasons: tuple[str, ...] = (),
    ) -> None:
        # Once per session, same reasoning as budget_exhausted: an outage is one
        # event for the conversation, however many turns it spans. ``reasons`` names
        # the cap case (item 4) on the row; a provider outage keeps no reason.
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
            reasons=list(reasons),
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
