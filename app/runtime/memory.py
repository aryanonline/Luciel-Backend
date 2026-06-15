"""Conversation memory + summarization cognition (Architecture §3.4.6 / §3.4.7).

Two cognition functions the loop wires in:

  Within-session memory (§3.4.6): full conversation context for the live session
    (the transcript rows for this session).

  Cross-session memory (§3.4.6): prior-session SUMMARIES for the same customer
    identifier — never the full prior transcripts (§3.4.8 reopened threads). This
    is gated by the HARD RULE in §3.4.9: cross-session memory is retrievable only
    for strong / platform-scoped-strong identifiers; a weak widget token never
    inherits history. The gate is sessions.can_carry_cross_session_memory.

  Summarization (§3.4.7): at session close (end signal or inactivity timeout) the
    runtime produces a structured summary (intent, key facts, actions, next step),
    persisted to the conversation record and available as prior-session context.
    Summarization runs on every conversation on every tier — not admin-configurable.

Improvement is through CONTEXT, never model modification (§3.4.6): no tenant
conversation is used to train/fine-tune weights. These functions only read/write
the per-tenant context store; they never touch a model artifact.

The summarizer here is a deterministic, dependency-free structurer so the loop +
tests run without an LLM. The production summarizer plugs in behind summarize().
A summary is constrained to facts present in the transcript (§3.4.13 / §3.4.7) —
this deterministic version only echoes transcript content, so it cannot introduce
unsupported facts by construction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import SessionSummary, Transcript
from app.runtime.sessions import can_carry_cross_session_memory


async def load_session_history(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> list[Transcript]:
    """Within-session memory (§3.4.6): the ordered transcript for the live session."""
    rows = (
        (
            await session.execute(
                select(Transcript)
                .where(
                    Transcript.admin_id == admin_id,
                    Transcript.session_id == session_id,
                )
                .order_by(Transcript.at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def load_cross_session_summaries(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    resolved_lead_id: uuid.UUID | None,
    exclude_session_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[str]:
    """Cross-session memory (§3.4.6): prior-session summaries for this customer.

    Returns [] when the channel identifier is weak (HARD RULE §3.4.9) or no lead is
    resolved — a weak widget token never inherits another lead's history. Summaries
    are scoped by (admin_id, instance_id, resolved_lead_id); RLS additionally scopes
    by admin_id so cross-tenant bleed is impossible.
    """
    if resolved_lead_id is None:
        return []
    # HARD RULE §3.4.9: only strong / platform-scoped-strong identifiers carry memory.
    if not can_carry_cross_session_memory(channel):
        return []
    q = (
        select(SessionSummary)
        .where(
            SessionSummary.admin_id == admin_id,
            SessionSummary.instance_id == instance_id,
            SessionSummary.resolved_lead_id == resolved_lead_id,
        )
        .order_by(SessionSummary.created_at.desc())
        .limit(limit)
    )
    if exclude_session_id is not None:
        q = q.where(SessionSummary.session_id != exclude_session_id)
    rows = (await session.execute(q)).scalars().all()
    return [r.summary for r in rows]


def summarize(history: list[Transcript]) -> str:
    """Structured conversation summary (§3.4.7): intent, key facts, actions, next step.

    Deterministic, transcript-grounded structurer (§3.4.13: no facts the transcript
    does not contain). The production summarizer binds behind the same signature.
    """
    if not history:
        return "No conversation content."
    lead_msgs = [t.text for t in history if t.role == "lead"]
    luciel_msgs = [t.text for t in history if t.role in ("luciel", "human_agent")]
    intent = lead_msgs[0].strip() if lead_msgs else "(no lead message)"
    last_action = luciel_msgs[-1].strip() if luciel_msgs else "(no response)"
    # Truncate to keep the persisted summary bounded (one-line cognition summary).
    intent = (intent[:160] + "…") if len(intent) > 160 else intent
    last_action = (last_action[:160] + "…") if len(last_action) > 160 else last_action
    return f"Intent: {intent} | Last response: {last_action} | Turns: {len(history)}"


async def persist_summary(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    summary: str,
) -> SessionSummary:
    """Persist the session summary (§3.4.7) to the conversation record + summary store.

    Writes a session_summaries row (available as prior-session context, §3.4.6) and
    stamps the session.summary one-liner shown in the dashboard conversation list.
    """
    from app.models.conversation import Session as SessionModel

    row = SessionSummary(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        resolved_lead_id=resolved_lead_id,
        session_id=session_id,
        summary=summary,
    )
    session.add(row)
    sess = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    if sess is not None:
        sess.summary = summary
    await session.flush()
    return row
