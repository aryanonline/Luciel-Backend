"""Transcript hot→cold archival at the 90-day boundary (Commitment #38, §3.4.10).

Until this sweep existed, transcripts sat in hot RDS storage — and every nightly
backup — for their full 365-day retention. Commitment #38's ratified model is that
they MOVE to S3 cold storage at 90 days and hard-delete at 1 year, so BOTH stores
plateau: the hot table holds at most ~90 days of transcript rows, the cold bucket at
most ~9 further months of JSONL objects.

Mechanics (per session, one transaction):
  1. Serialize the session's transcript rows to JSONL (id, role, text, at,
     grounding_score, source_chunks — everything the dashboard evidence view needs).
  2. ``store.put`` the object (overwrite-idempotent on the session-keyed name).
  3. Only after the put returns: stamp ``transcripts_archived_at`` +
     ``archive_object_key`` and DELETE the hot rows. A put failure aborts before any
     stamp/delete, leaving everything hot for the next run — at-least-once safe.

Only ENDED sessions are archived (``ended_at IS NOT NULL``): a session the
finalization sweep hasn't sealed can still receive turns, and archiving a live
transcript would fork it across two stores. In practice a 90-day-idle session is
long past every inactivity window, so the ended gate costs nothing and guards the
invariant. Zero-turn sessions are stamped (with no object) so the sweep never
re-visits them.

Deletion of the archived object at the 1-year boundary is the retention sweep's job
(app/lifecycle/retention.py — the authoritative deleter); the bucket's own
365-days-from-upload lifecycle rule is only the late-safe backstop (data-stack.ts).
The hard-delete cascade removes the whole tenant prefix (cascade.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.lifecycle.transcript_store import get_transcript_store, transcript_key_for
from app.models.audit import ActorType
from app.models.conversation import Session, Transcript

#: The hot boundary (Commitment #38): transcripts older than this move to cold S3.
ARCHIVE_AFTER = timedelta(days=90)


def serialize_transcripts(rows: list[Transcript]) -> bytes:
    """JSONL, one row per line, ordered by time — the archive's on-disk shape.

    Carries every field the read paths need (dashboard messages + evidence states),
    so serving an archived conversation is lossless vs. the hot rows."""
    lines = [
        json.dumps(
            {
                "id": str(r.id),
                "session_id": str(r.session_id),
                "role": r.role,
                "text": r.text,
                "at": r.at.isoformat(),
                "grounding_score": r.grounding_score,
                "source_chunks": r.source_chunks,
            },
            ensure_ascii=False,
        )
        for r in sorted(rows, key=lambda r: r.at)
    ]
    return ("\n".join(lines) + "\n").encode()


def deserialize_transcripts(data: bytes) -> list[dict[str, object]]:
    """Parse an archived JSONL object back into row dicts (time-ordered)."""
    out: list[dict[str, object]] = []
    for line in data.decode().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


async def archive_aged_transcripts(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Move every ended session's transcripts past the 90-day boundary to cold S3.

    Returns the number of sessions archived (the sweep's acted signal). Idempotent:
    archived sessions carry ``transcripts_archived_at`` and are never candidates
    again; a crash between put and stamp re-puts the same key (overwrite) next run.

    REFUSES TO RUN against a non-durable store (``store.durable is False``). This is
    the load-bearing safety check, found on dev: in mock mode the bound store is an
    in-process dict, so a sweep running as its own one-shot process deleted the hot
    rows, wrote the "archive" into memory that died with it, and left the session
    pointing at a key resolving to nothing — a silent, permanent loss of the
    customer's conversation. "Move" is only a move when the destination outlives the
    mover; otherwise this is a no-op and the transcripts stay hot where they are safe.
    """
    now = now or datetime.now(UTC)
    store = get_transcript_store()
    if not getattr(store, "durable", False):
        return 0
    candidates = (
        (
            await session.execute(
                select(Session).where(
                    Session.admin_id == admin_id,
                    Session.ended_at.is_not(None),
                    Session.transcripts_archived_at.is_(None),
                    Session.last_message_at < now - ARCHIVE_AFTER,
                )
            )
        )
        .scalars()
        .all()
    )
    archived = 0
    for sess in candidates:
        rows = (
            (
                await session.execute(
                    select(Transcript).where(
                        Transcript.admin_id == admin_id,
                        Transcript.session_id == sess.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if rows:
            key = transcript_key_for(admin_id, sess.instance_id, sess.id)
            # Durable put FIRST; only then stamp + delete the hot rows. A put failure
            # raises here and aborts the transaction — everything stays hot.
            await store.put(key=key, data=serialize_transcripts(list(rows)))
            sess.archive_object_key = key
            await session.execute(
                delete(Transcript).where(
                    Transcript.admin_id == admin_id, Transcript.session_id == sess.id
                )
            )
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=sess.instance_id,
                actor_type=ActorType.platform,
                event_type="transcript_archived",
                # No content, no PII — the object key + count only (§3.4.11).
                payload={
                    "session_id": str(sess.id),
                    "row_count": len(rows),
                    "object_key": key,
                },
            )
        # Zero-turn sessions are stamped with no object so the sweep moves on.
        sess.transcripts_archived_at = now
        archived += 1
    if archived:
        await session.flush()
    return archived


async def load_archived_transcripts(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> list[dict[str, object]] | None:
    """Read-through for an archived session's transcript, or None if not archived.

    Used by the dashboard conversation view and the export bundle so a conversation
    older than 90 days is served on demand from cold storage rather than going blank
    the day its hot rows moved."""
    sess = (
        await session.execute(
            select(Session).where(Session.admin_id == admin_id, Session.id == session_id)
        )
    ).scalar_one_or_none()
    if sess is None or not sess.archive_object_key:
        return None
    data = await get_transcript_store().get(key=sess.archive_object_key)
    if data is None:
        return None
    return deserialize_transcripts(data)
