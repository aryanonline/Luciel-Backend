"""The export bundle's per-session reads state the tenant predicate (round 7 WP-5, item 4).

RLS scopes every read already; the explicit ``admin_id`` is the defence in depth every
other tenant read in ``export_bundle`` carries. The transcript and summary reads were
the two that keyed on ``session_id`` alone, so a session id from anywhere could in
principle widen a bundle if the tenant session were ever the wrong one.
"""

from __future__ import annotations

import uuid

from app.lifecycle.export_bundle import summary_stmt, transcripts_stmt
from app.models.conversation import SessionSummary, Transcript


def test_the_transcript_read_is_tenant_scoped_and_time_ordered() -> None:
    admin, sid = uuid.uuid4(), uuid.uuid4()
    compiled = transcripts_stmt(admin, sid).compile()
    sql = str(compiled)
    assert f"{Transcript.__tablename__}.admin_id = " in sql
    assert f"{Transcript.__tablename__}.session_id = " in sql
    assert "ORDER BY" in sql
    assert {admin, sid} <= set(compiled.params.values())


def test_the_summary_read_is_tenant_scoped() -> None:
    admin, sid = uuid.uuid4(), uuid.uuid4()
    compiled = summary_stmt(admin, sid).compile()
    sql = str(compiled)
    assert f"{SessionSummary.__tablename__}.admin_id = " in sql
    assert f"{SessionSummary.__tablename__}.session_id = " in sql
    assert {admin, sid} <= set(compiled.params.values())
