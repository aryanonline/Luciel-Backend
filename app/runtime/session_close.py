"""Session finalization — the deterministic session END (§3.4.7 / §3.4.8).

A session ends in exactly two ways, neither of them an LLM judgment:

  (a) Inactivity timeout, by channel class (``INACTIVITY_TIMEOUT``) — enforced here by
      the ``session_finalize`` sweep (app/jobs/sweeps.py), which stamps ``ended_at``
      and fires the §3.4.7 summarization pipeline for every session past its window.
  (b) An explicit end signal where the channel provides one — the widget close beacon
      (POST /sessions/{id}/end) and the voice hangup (the Twilio status callback,
      POST /webhooks/twilio/voice/status) — via :func:`finalize_session`.

Until this module existed, session end was purely implicit (an expired
``last_message_at``): nothing ever summarized a conversation, so ``sessions.summary``
stayed NULL, the cross-session memory store stayed empty, and the §3.4.6 "Luciel
remembers you" promise was silently unimplemented. The sweep is what makes
summarization actually run "at the close of every conversation" (§3.4.7).

``ended_at`` is stamped at ``last_message_at + timeout`` — the honest boundary the
§3.4.8 rule defines — not at whatever moment the sweep happened to run.

Idempotent by construction: only ``ended_at IS NULL`` rows are candidates, and the
stamp + summary write share one transaction; a second run finds nothing to do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType, AdminAuditLog
from app.models.conversation import Session
from app.runtime import memory
from app.runtime.sessions import INACTIVITY_TIMEOUT

# No session is due before the SHORTEST channel window has passed; pre-filtering on it
# keeps the sweep's candidate query narrow. (Unknown/legacy channels use the same
# 30-minute default the resolver applies.)
_DEFAULT_TIMEOUT = timedelta(minutes=30)
_MIN_TIMEOUT = min([*INACTIVITY_TIMEOUT.values(), _DEFAULT_TIMEOUT])


def _timeout_for(channel: str) -> timedelta:
    return INACTIVITY_TIMEOUT.get(channel, _DEFAULT_TIMEOUT)


async def takeover_duration_seconds(
    session: AsyncSession, *, admin_id: uuid.UUID, sess: Session, ended_at: datetime
) -> int | None:
    """How long a person held ``sess`` (round 7 WP-2, item 9): from the session's
    latest ``human_takeover_started`` audit event — the persisted proof of when the
    take-over began — falling back to the session's own start when no event exists
    (a row set human_controlled before the trail was recorded). None only when
    neither is known."""
    started = (
        await session.execute(
            select(AdminAuditLog.created_at)
            .where(
                AdminAuditLog.admin_id == admin_id,
                AdminAuditLog.event_type == "human_takeover_started",
                AdminAuditLog.event_payload["session_id"].astext == str(sess.id),
            )
            .order_by(AdminAuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    origin = started or sess.started_at
    if origin is None:
        return None
    return max(0, int((ended_at - origin).total_seconds()))


async def _summarize_and_stamp(session: AsyncSession, sess: Session, *, ended_at: datetime) -> None:
    """Seal one session: stamp ``ended_at``; summarize + persist when it has content.

    A zero-turn session (opened but never transcribed — e.g. an at-cap intake reply)
    still gets ``ended_at`` so it can never be resumed, but no summary row: an empty
    conversation has nothing to remember, and a "No conversation content." row would
    be pure noise in the cross-session store.
    """
    sess.ended_at = ended_at
    if sess.mode == "human_controlled":
        # The takeover ended with the session, not with a hand-back (§3.4.12 — "if
        # the admin never hands back, the session ends via the inactivity timeout").
        # Audit F078: this end was never recorded, so the takeover audit trail had a
        # start for every timeout and no end. Round 7 WP-2 item 9: the end carries
        # how long the person held it, and the sealed row returns to ai mode —
        # hand-back was the only writer, so a stale human_controlled outlived the
        # session and read as an open take-over on every dashboard that lists it.
        await audit.emit(
            session,
            admin_id=sess.admin_id,
            instance_id=sess.instance_id,
            event_type="human_takeover_ended",
            actor_type=ActorType.platform,
            payload={
                "session_id": str(sess.id),
                "channel": sess.channel,
                "trigger": "session_timeout",
                "resolved_lead_id_hash": (
                    audit.hash_lead_id(sess.resolved_lead_id) if sess.resolved_lead_id else None
                ),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": await takeover_duration_seconds(
                    session, admin_id=sess.admin_id, sess=sess, ended_at=ended_at
                ),
            },
        )
        sess.mode = "ai"
    history = await memory.load_session_history(session, admin_id=sess.admin_id, session_id=sess.id)
    if not history:
        await session.flush()
        return
    await memory.persist_summary(
        session,
        admin_id=sess.admin_id,
        instance_id=sess.instance_id,
        session_id=sess.id,
        resolved_lead_id=sess.resolved_lead_id,
        summary=memory.summarize(history),
    )


async def finalize_due_sessions(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Close every session past its channel inactivity window (§3.4.8 trigger (a)).

    Returns the number of sessions finalized (the sweep's acted signal).
    """
    now = now or datetime.now(UTC)
    candidates = (
        (
            await session.execute(
                select(Session).where(
                    Session.admin_id == admin_id,
                    Session.ended_at.is_(None),
                    # Narrowing pre-filter: nothing younger than the shortest channel
                    # window can be due; the per-channel check below is authoritative.
                    Session.last_message_at < now - _MIN_TIMEOUT,
                )
            )
        )
        .scalars()
        .all()
    )
    finalized = 0
    for sess in candidates:
        timeout = _timeout_for(sess.channel)
        boundary = sess.last_message_at + timeout
        if boundary > now:
            continue  # inside its own (longer) window — not due yet
        await _summarize_and_stamp(session, sess, ended_at=boundary)
        finalized += 1
    if finalized:
        await session.flush()
    return finalized


async def finalize_session(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    session_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Explicit end signal (§3.4.8 trigger (b)) — widget close beacon / voice hangup.

    Scoped to (admin, instance, channel) so a data-plane caller can only ever end its
    own tenant's session on its own channel. Idempotent: an already-ended session is
    a no-op (False). Ends at ``now`` — the user's own signal, not the timeout.
    """
    now = now or datetime.now(UTC)
    sess = (
        await session.execute(
            select(Session).where(
                Session.admin_id == admin_id,
                Session.instance_id == instance_id,
                Session.channel == channel,
                Session.id == session_id,
                Session.ended_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        return False
    await _summarize_and_stamp(session, sess, ended_at=now)
    await session.flush()
    return True
