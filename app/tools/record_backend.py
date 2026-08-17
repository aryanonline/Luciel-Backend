"""Record-source backends — the effect layer behind ``lookup_record`` (§3.3.6).

The ``csv`` provider's live system of record is the admin's uploaded rows
(``record_source_records``), queried AT CALL TIME through the tool's own session
— never a knowledge chunk, never a cached snapshot (§3.2 boundary). Outcomes
follow the honest vocabulary exactly: 0 matches → ``empty`` (a true statement
about the admin's data), 1 → ``ok``, several → ``ambiguous`` with candidates.

Matching is deterministic and explainable: a row matches when EVERY word of the
query appears (case-insensitive substring) somewhere in the row's field values.
No fuzzy scoring — an exact-record tool that "kind of" matches would put the
loop in the position of narrating a guess (§3.3.4 faithful readback).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.record_source import RecordSourceRecord
from app.tools.contract import ToolContext

# ambiguous candidates are for the customer to disambiguate between — a page of
# them is a search result, not a disambiguation. Cap small, deterministically.
AMBIGUOUS_CANDIDATE_CAP = 5


class RecordSourceUnavailable(RuntimeError):
    """The record source cannot be read right now — surfaces as ``failed``."""


class CsvRecordBackend:
    """``RecordBackend`` over the tenant's uploaded CSV rows."""

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        if ctx.session is None:
            raise RecordSourceUnavailable("record_source_session_unavailable")
        rows = (
            (
                await ctx.session.execute(
                    select(RecordSourceRecord)
                    .where(
                        RecordSourceRecord.admin_id == ctx.admin_id,
                        RecordSourceRecord.instance_id == ctx.instance_id,
                    )
                    .order_by(RecordSourceRecord.ordinal)
                )
            )
            .scalars()
            .all()
        )
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []
        matches: list[dict[str, Any]] = []
        for row in rows:
            # Keys AND values: the admin's column names carry meaning ("package",
            # "price") and queries naturally mention them ("the starter package").
            haystack = " ".join(
                f"{str(k).lower()} {str(v).lower()}" for k, v in (row.fields or {}).items()
            )
            if all(token in haystack for token in tokens):
                matches.append(dict(row.fields or {}))
                if len(matches) > AMBIGUOUS_CANDIDATE_CAP:
                    # Already unambiguously ambiguous; a bounded list is enough
                    # for the loop to ask the customer to narrow down.
                    break
        return matches
