"""The archive's on-disk shape is a decision per column (round 7 phase 2 review, seams 6).

``serialize_transcripts`` writes the ONLY copy of a transcript once the 90-day sweep
deletes the hot rows, and its docstring promised "lossless vs. the hot rows" while
dropping the delivery outcome (migration 0066) and the owner's answer flag (0070): a
rejected human reply read as an ordinary message after the move, the flag was gone,
and the ``deterministic`` marker (0051) that tells "scoring never applied" from "this
row predates scoring" went with them. Nothing but the audit log held any of it.

The walk below requires every column of the transcripts model to be either carried by
the serializer (proved on a row with EVERY column populated) or listed here as NOT
archived with the reason, so a column added later fails until someone decides.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.lifecycle.transcript_archive import deserialize_transcripts, serialize_transcripts
from app.models.conversation import Transcript

#: Columns the archive deliberately does not carry, with the reason each.
_NOT_ARCHIVED: dict[str, str] = {
    "admin_id": "the tenant is the object key's prefix (transcript_key_for); RLS scopes the read",
    "instance_id": "in the object key too, and on the session row the archive hangs off",
    "created_at": "row insertion time; 'at' is the message's own time and is archived",
}

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def _row(**overrides: object) -> Transcript:
    """A human reply with EVERY archived column populated (no NULL could pass the walk
    by accident). Overrides make the other roles."""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "admin_id": uuid.uuid4(),
        "instance_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "role": "human_agent",
        "text": "We open at 9 on Sundays.",
        "at": NOW,
        "grounding_score": 0.91,
        "source_chunks": [{"source_id": str(uuid.uuid4()), "source_name": "Hours", "text": "9-5"}],
        "deterministic": True,
        "delivery_status": "failed",
        "delivery_detail": "send_failed",
        "delivered_at": NOW + timedelta(seconds=1),
        "flagged_by_admin_at": NOW + timedelta(minutes=5),
    }
    fields.update(overrides)
    return Transcript(**fields)


def test_every_transcript_column_is_either_archived_or_deliberately_not() -> None:
    row = _row()
    (archived,) = deserialize_transcripts(serialize_transcripts([row]))
    columns = {c.name for c in Transcript.__table__.columns}

    stale = set(_NOT_ARCHIVED) - columns
    assert not stale, f"no longer on the model: {sorted(stale)}"
    assert all(reason.strip() for reason in _NOT_ARCHIVED.values())
    assert not set(_NOT_ARCHIVED) & set(archived), "listed as not archived yet serialized"

    missing = columns - set(_NOT_ARCHIVED) - set(archived)
    assert not missing, (
        f"transcripts column(s) {sorted(missing)} are not classified for the 90-day archive: "
        "serialize them in app/lifecycle/transcript_archive.py (the archive is the only "
        "copy once the hot rows are deleted), or add them to _NOT_ARCHIVED with the reason"
    )
    extra = set(archived) - columns
    assert not extra, f"serialized keys that are not columns: {sorted(extra)}"
    # Carried for real, not merely keyed: the walk seeded every archived column.
    null = [name for name in set(archived) - set(_NOT_ARCHIVED) if archived[name] is None]
    assert not null, f"archived as NULL although the row had a value: {sorted(null)}"


def test_the_delivery_outcome_the_flag_and_the_marker_survive_the_round_trip() -> None:
    row = _row()
    (archived,) = deserialize_transcripts(serialize_transcripts([row]))
    assert archived["delivery_status"] == "failed"
    assert archived["delivery_detail"] == "send_failed"
    assert archived["delivered_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert archived["flagged_by_admin_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert archived["deterministic"] is True
    # Timestamps are ISO strings with their offset, so a reader restores the instant.
    assert datetime.fromisoformat(str(archived["delivered_at"])) == NOW + timedelta(seconds=1)


def test_a_row_without_an_outcome_or_flag_archives_them_as_null_not_absent() -> None:
    """A lead's turn has no delivery outcome and was never flagged: the keys are
    present and null, the same shape the hot row has — a reader never has to guess
    whether a missing key means "none" or "written before the key existed"."""
    lead_turn = _row(
        role="lead",
        grounding_score=None,
        source_chunks=None,
        deterministic=False,
        delivery_status=None,
        delivery_detail=None,
        delivered_at=None,
        flagged_by_admin_at=None,
    )
    (line,) = serialize_transcripts([lead_turn]).decode().splitlines()
    parsed = json.loads(line)
    for key in ("delivery_status", "delivery_detail", "delivered_at", "flagged_by_admin_at"):
        assert key in parsed and parsed[key] is None
    assert parsed["deterministic"] is False


def test_rows_are_written_in_time_order_whatever_order_they_were_read() -> None:
    later = _row(at=NOW + timedelta(minutes=2), text="later")
    earlier = _row(at=NOW, text="earlier")
    archived = deserialize_transcripts(serialize_transcripts([later, earlier]))
    assert [r["text"] for r in archived] == ["earlier", "later"]
