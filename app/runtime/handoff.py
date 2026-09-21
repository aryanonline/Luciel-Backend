"""Human handoff & live takeover cognition (DOCTRINE ANCHOR — Architecture §3.4.12).

Always-on cognition (not a configurable tool). Two ways a session enters
human_controlled mode:
  1. Luciel-initiated: the escalation module fires explicit_human_request (gate 1
     — intake, §3.4.5). Luciel acknowledges, the escalation_fired event is written,
     and the session is set to human_controlled.
  2. Admin-initiated live takeover: the admin "Take over" in the dashboard.

While human_controlled (§3.4.12):
  - The agentic loop STOPS auto-responding on that session — inbound messages are
    delivered to the dashboard live feed but do NOT invoke the loop (no LLM calls).
  - Budget: a human-controlled session does not consume LLM budget for human turns,
    but the session still counts as ONE conversation against the budget (consistent
    with §3.4.1b / Locked Decision §7 #33). If takeover happens BEFORE any model
    call, the moment it becomes human-controlled is the fire-once budget trigger.

Returning control (§3.4.12): the admin "Hand back" transitions human_controlled →
ai. If the admin never hands back, the session ends via the channel inactivity
timeout (§3.4.8); on the next inbound a new session opens with the prior-session
summary as context (§3.4.6).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import Session as SessionModel
from app.runtime.budget import count_session_once


class ConversationMode(str, enum.Enum):
    ai = "ai"
    human_controlled = "human_controlled"


#: ``human_takeover_started.trigger`` values (round 7 WP-3b): WHO decided a person
#: takes the conversation. Luciel's own escalation judgment vs. the owner's button.
TRIGGER_LUCIEL_ESCALATED = "luciel_escalated"
TRIGGER_ADMIN_INITIATED = "admin_initiated"


class SessionEnded(Exception):
    """A takeover was asked for on a session that already ENDED (``ended_at`` set).

    An ended session is never resumed (§3.4.8) and the customer's transport is no
    longer waiting on it, so switching it to human_controlled would be a takeover of
    nothing — the dashboard would show a person "holding" a conversation nobody can
    answer into. Refused, never fabricated (round 7 WP-3b, item 4)."""

    def __init__(self, session_id: uuid.UUID) -> None:
        self.session_id = session_id
        super().__init__("session_ended")


def loop_should_run(mode: str) -> bool:
    """The agentic loop runs only while the session is in ai mode (§3.4.12).

    human_controlled STOPS the loop — inbound goes to the dashboard live feed, no
    LLM call is made for the customer on that session."""
    return mode == ConversationMode.ai.value


async def take_over(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    luciel_initiated: bool = False,
) -> SessionModel:
    """Transition a session into human_controlled mode (§3.4.12).

    Idempotent: taking over an already-human_controlled session is a no-op beyond
    re-asserting the mode. The session still counts as one budget unit — if no model
    call has fired yet, becoming human_controlled is the fire-once budget trigger
    (§3.4.1b).

    Raises ``SessionEnded`` for a session whose ``ended_at`` is set (round 7 WP-3b):
    nothing is waiting on it, so there is nothing to take over."""
    sess = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        raise LookupError("session_not_found")
    if sess.ended_at is not None:
        raise SessionEnded(session_id)

    already = sess.mode == ConversationMode.human_controlled.value
    sess.mode = ConversationMode.human_controlled.value
    if not already:
        # The start of THIS takeover — what hand-back's duration_seconds is measured
        # from (persisted proof, not a guess from the audit trail). A re-asserted
        # takeover keeps its original start; a pre-0066 row that never had one stays
        # NULL and hands back with an unknown duration rather than a fabricated one.
        sess.human_controlled_since = datetime.now(UTC)

    # Budget: one session = one conversation. Becoming human-controlled before any
    # model call is the fire-once trigger (§3.4.1b Counter precision / §7 #33).
    counted = await count_session_once(
        session, admin_id=admin_id, instance_id=instance_id, session_id=session_id
    )
    if counted:
        # The takeover path is a counting path, so it is a metering path too (audit
        # F043): a conversation that crossed a block boundary here used to be counted
        # but never reported until some later model-call turn happened to run.
        from app.billing import metering, notice_delivery  # noqa: PLC0415 — import cycle

        await metering.report_usage_after_count(session, admin_id=admin_id, instance_id=instance_id)
        # Adversarial review R-WP1-2: the block a takeover opened was announced only
        # by the daily sweep, while a model-call turn announces it the moment the
        # count moves. Same event, same moment, whichever path counted. Never raises.
        await notice_delivery.send_usage_notices_after_count(
            session, admin_id=admin_id, now=datetime.now(UTC)
        )

    if not already:
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="human_takeover_started",
            actor_type=ActorType.luciel_runtime if luciel_initiated else ActorType.admin_user,
            actor_user_id=actor_user_id,
            # No raw lead PII in the payload (§3.4.11) — ids + trigger + channel only.
            payload={
                "session_id": str(session_id),
                "luciel_initiated": luciel_initiated,
                "trigger": (
                    TRIGGER_LUCIEL_ESCALATED if luciel_initiated else TRIGGER_ADMIN_INITIATED
                ),
                "channel": sess.channel,
                "resolved_lead_id_hash": (
                    audit.hash_lead_id(sess.resolved_lead_id) if sess.resolved_lead_id else None
                ),
            },
        )
    await session.flush()
    return sess


async def hand_back(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> SessionModel:
    """Transition human_controlled → ai (admin "Hand back", §3.4.12).

    The session returns to the normal agentic state; the budget is unchanged (it was
    already counted once). Idempotent on an already-ai session.

    The end event carries ``duration_seconds`` — how long a person held the
    conversation, measured from the persisted ``human_controlled_since`` (round 7
    WP-3b, item 8). ``None`` when the takeover predates that column: unknown is
    reported as unknown, never as zero."""
    sess = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        raise LookupError("session_not_found")

    was_human = sess.mode == ConversationMode.human_controlled.value
    sess.mode = ConversationMode.ai.value
    if was_human:
        now = datetime.now(UTC)
        duration_seconds = takeover_duration_seconds(sess.human_controlled_since, now=now)
        sess.human_controlled_since = None
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="human_takeover_ended",
            actor_type=ActorType.admin_user,
            actor_user_id=actor_user_id,
            payload={
                "session_id": str(session_id),
                "trigger": "hand_back",
                "channel": sess.channel,
                "duration_seconds": duration_seconds,
                "resolved_lead_id_hash": (
                    audit.hash_lead_id(sess.resolved_lead_id) if sess.resolved_lead_id else None
                ),
            },
        )
    await session.flush()
    return sess


def takeover_duration_seconds(since: datetime | None, *, now: datetime) -> int | None:
    """Whole seconds a takeover has run, or ``None`` when its start was never recorded.

    Shared so the session-close path can report the same number off the same column
    when a takeover ends with the session instead of a hand-back. Clock skew can put
    ``since`` a hair after ``now``; that clamps to 0 rather than going negative."""
    if since is None:
        return None
    return max(0, int((now - since).total_seconds()))
