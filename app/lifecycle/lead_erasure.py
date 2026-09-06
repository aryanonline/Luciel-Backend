"""Right-to-erasure cascade for ONE lead (Architecture §3.4.11, Legal §B5).

The single helper every lead-destroying path uses — the dashboard "erase this
lead", the dashboard prune, and the opt-in auto-prune sweep. Before this module
(2026-09-05 audit F091/F018) the three paths disagreed: prune deleted only the
lead row (transcripts and summaries survived under a dead lead id), and even the
erase left the archived S3 transcript, the session's summary text, the session's
transport key and the voice call record's number behind. "Erasing here removes
everything we hold" is the product's own copy; this is what makes it true.

What goes, per lead:
  * hot transcript rows of every session resolved to the lead;
  * the cold-storage transcript object of each such session (best-effort against
    S3 — a store outage is counted and logged, never swallowed silently; the
    bucket's lifecycle backstop still bounds the object);
  * the session summaries written for the lead;
  * the session rows are ANONYMISED, not deleted: summary text, transport key and
    the lead link go, the row (channel, timings, counted flag) stays — it is the
    billing/analytics record and carries no personal data once those are gone;
  * voice call records for those sessions lose the caller's number (the
    disclosure-played proof is compliance evidence and stays);
  * deferred follow-ups addressed to the person (their contact rides in the
    payload; a queued callback to an erased person must not fire);
  * the CRM link rows (plaintext email, round 5A item 2);
  * the lead row itself.
The audit event carries only the HASHED lead id (§3.4.11 / §5.3).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.lifecycle.transcript_store import get_transcript_store
from app.models.audit import ActorType
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.deferred import DeferredAction
from app.models.lead import Lead
from app.models.voice import VoiceCallRecord

_log = logging.getLogger(__name__)


@dataclass
class ErasureCounts:
    transcripts_deleted: int = 0
    archived_objects_deleted: int = 0
    archived_object_errors: int = 0
    summaries_deleted: int = 0
    sessions_anonymised: int = 0
    voice_records_scrubbed: int = 0
    deferred_actions_deleted: int = 0
    crm_links_deleted: int = 0
    identifiers_deleted: int = 0

    def payload(self) -> dict[str, object]:
        return dict(asdict(self))


async def erase_lead_data(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    lead: Lead,
    event_type: str,
    actor_type: ActorType,
    actor_user_id: uuid.UUID | None = None,
    extra_payload: dict[str, object] | None = None,
) -> ErasureCounts:
    """Destroy everything the platform holds about ``lead`` and audit it once.

    ``event_type`` is ``lead_erasure`` for the owner's explicit erase and
    ``lead_pruned`` for prune/auto-prune — the cascade is identical; only the
    reason recorded differs. Must run inside the tenant's session (RLS scopes
    every statement; the explicit ``admin_id`` filters are the belt to its braces).
    """
    from app.tools.crm_backend import delete_links_for_lead  # noqa: PLC0415 — import cycle

    counts = ErasureCounts()
    lead_id = lead.id
    instance_id = lead.instance_id

    sessions = (
        (
            await session.execute(
                select(Session).where(
                    Session.admin_id == admin_id, Session.resolved_lead_id == lead_id
                )
            )
        )
        .scalars()
        .all()
    )
    session_ids = [s.id for s in sessions]

    if session_ids:
        result = await session.execute(
            delete(Transcript).where(
                Transcript.admin_id == admin_id, Transcript.session_id.in_(session_ids)
            )
        )
        counts.transcripts_deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]

        store = get_transcript_store()
        for sess in sessions:
            if sess.archive_object_key:
                try:
                    await store.delete(key=sess.archive_object_key)
                    counts.archived_objects_deleted += 1
                except Exception:  # noqa: BLE001 — counted + logged, never silent
                    counts.archived_object_errors += 1
                    _log.exception(
                        "erasure: archived transcript delete failed admin_id=%s session=%s",
                        admin_id,
                        sess.id,
                    )
                sess.archive_object_key = None
            sess.summary = None
            sess.participant_key = None
            sess.resolved_lead_id = None
            counts.sessions_anonymised += 1

        voice = await session.execute(
            update(VoiceCallRecord)
            .where(
                VoiceCallRecord.admin_id == admin_id,
                VoiceCallRecord.session_id.in_(session_ids),
                VoiceCallRecord.from_number != "",
            )
            .values(from_number="")
        )
        counts.voice_records_scrubbed = int(voice.rowcount or 0)  # type: ignore[attr-defined]

    summaries = await session.execute(
        delete(SessionSummary).where(
            SessionSummary.admin_id == admin_id, SessionSummary.resolved_lead_id == lead_id
        )
    )
    counts.summaries_deleted = int(summaries.rowcount or 0)  # type: ignore[attr-defined]

    deferred = await session.execute(
        delete(DeferredAction).where(
            DeferredAction.admin_id == admin_id, DeferredAction.resolved_lead_id == lead_id
        )
    )
    counts.deferred_actions_deleted = int(deferred.rowcount or 0)  # type: ignore[attr-defined]

    counts.crm_links_deleted = await delete_links_for_lead(session, admin_id=admin_id, lead=lead)
    from app.models.lead import LeadIdentifier  # noqa: PLC0415

    identifiers = await session.execute(
        delete(LeadIdentifier).where(
            LeadIdentifier.admin_id == admin_id, LeadIdentifier.lead_id == lead_id
        )
    )
    counts.identifiers_deleted = int(identifiers.rowcount or 0)  # type: ignore[attr-defined]
    await session.delete(lead)
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        # No raw PII in the chained payload — the HASHED lead id, so erasure-by-key-
        # destruction holds against the immutable log (§3.4.11 / §5.3).
        payload={
            "resolved_lead_id_hash": audit.hash_lead_id(lead_id),
            **counts.payload(),
            **(extra_payload or {}),
        },
    )
    if counts.archived_object_errors:
        # Feeds the sweep/app error alarms (audit F041 posture): a cold-storage
        # object that outlived an erasure is an operator's problem, not a footnote.
        _log.error(
            "erasure left %d archived transcript object(s) behind admin_id=%s lead_hash=%s",
            counts.archived_object_errors,
            admin_id,
            audit.hash_lead_id(lead_id),
        )
    return counts
