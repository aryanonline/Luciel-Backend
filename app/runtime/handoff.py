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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import Session as SessionModel
from app.runtime.budget import count_session_once


class ConversationMode(str, enum.Enum):
    ai = "ai"
    human_controlled = "human_controlled"


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
    (§3.4.1b)."""
    sess = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        raise LookupError("session_not_found")

    already = sess.mode == ConversationMode.human_controlled.value
    sess.mode = ConversationMode.human_controlled.value

    # Budget: one session = one conversation. Becoming human-controlled before any
    # model call is the fire-once trigger (§3.4.1b Counter precision / §7 #33).
    counted = await count_session_once(
        session, admin_id=admin_id, instance_id=instance_id, session_id=session_id
    )
    if counted:
        # The takeover path is a counting path, so it is a metering path too (audit
        # F043): a conversation that crossed a block boundary here used to be counted
        # but never reported until some later model-call turn happened to run.
        from app.billing import metering  # noqa: PLC0415 — avoids an import cycle

        await metering.report_usage_after_count(session, admin_id=admin_id, instance_id=instance_id)

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
                "trigger": "luciel" if luciel_initiated else "admin",
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
    already counted once). Idempotent on an already-ai session."""
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
                "resolved_lead_id_hash": (
                    audit.hash_lead_id(sess.resolved_lead_id) if sess.resolved_lead_id else None
                ),
            },
        )
    await session.flush()
    return sess
