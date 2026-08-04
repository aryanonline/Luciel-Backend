"""Unit tests for app/analytics/service.py (Architecture §3.9).

Exercises the real aggregate queries directly against the test DB (busiest-times
heatmap, channel mix, escalations-by-signal, conversion-by-channel) and asserts
the honest-gap markers for appointments-booked / response-time-percentiles /
top-knowledge-sources / conversion-by-source/service are NEVER silently
fabricated as 0/[] — they carry an explicit "not available" reason instead.
Also covers CSV rendering for every view in CSV_VIEWS.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime

import pytest
from app.analytics import service as analytics_service
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Session
from app.models.instance import Instance, LucielState
from app.models.lead import Lead
from app.runtime.budget import current_billing_period_start


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


async def _add_session(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    channel: str,
    started_at: datetime,
    resolved_lead_id: uuid.UUID | None = None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Session(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel=channel,
                mode="ai",
                started_at=started_at,
                last_message_at=started_at,
                resolved_lead_id=resolved_lead_id,
            )
        )
    return sid


async def _add_lead(admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    lid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(Lead(id=lid, admin_id=admin, instance_id=iid, name="A Lead"))
    return lid


async def _add_escalation(admin: uuid.UUID, iid: uuid.UUID, *, signal: str) -> None:
    async with tenant_session(admin) as s:
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                signal=signal,
                gate="intake",
                fired_at=datetime.now(UTC),
            )
        )


@pytest.mark.usefixtures("db_engine_reset")
async def test_busiest_times_is_real_heatmap() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin)
    # Tuesday 09:00 UTC, twice, and Wednesday 14:00 once.
    tue_9am = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)  # 2026-07-07 is a Tuesday
    wed_2pm = datetime(2026, 7, 8, 14, 0, tzinfo=UTC)
    await _add_session(admin, iid, channel="widget", started_at=tue_9am)
    await _add_session(admin, iid, channel="widget", started_at=tue_9am)
    await _add_session(admin, iid, channel="sms", started_at=wed_2pm)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=3,
        )

    assert agg.busiest_times[1][9] == 2  # Tuesday=1 (Python weekday()), 09:00
    assert agg.busiest_times[2][14] == 1  # Wednesday=2, 14:00
    assert sum(sum(row) for row in agg.busiest_times) == 3
    assert len(agg.busiest_times) == 7
    assert all(len(row) == 24 for row in agg.busiest_times)


@pytest.mark.usefixtures("db_engine_reset")
async def test_channel_mix_and_conversion_by_channel_are_real() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin)
    lead = await _add_lead(admin, iid)
    now = datetime.now(UTC)
    await _add_session(admin, iid, channel="widget", started_at=now, resolved_lead_id=lead)
    await _add_session(admin, iid, channel="widget", started_at=now)
    await _add_session(admin, iid, channel="sms", started_at=now)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=3,
        )

    mix_by_channel = {c.channel: c.fraction for c in agg.channel_mix}
    assert mix_by_channel["widget"] == pytest.approx(2 / 3)
    assert mix_by_channel["sms"] == pytest.approx(1 / 3)

    conv_by_channel = {c.channel: c for c in agg.conversion_by_channel}
    assert conv_by_channel["widget"].conversations == 2
    assert conv_by_channel["widget"].leads == 1
    assert conv_by_channel["widget"].conversion_rate == pytest.approx(0.5)
    assert conv_by_channel["sms"].leads == 0
    assert conv_by_channel["sms"].conversion_rate == 0.0


@pytest.mark.usefixtures("db_engine_reset")
async def test_escalations_by_signal_real_and_period_scoped() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin)
    await _add_escalation(admin, iid, signal="cannot_answer")
    await _add_escalation(admin, iid, signal="cannot_answer")
    await _add_escalation(admin, iid, signal="high_value_lead")

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=0,
        )

    counts = {e.signal: e.count for e in agg.escalations_by_signal}
    assert counts["cannot_answer"] == 2
    assert counts["high_value_lead"] == 1


@pytest.mark.usefixtures("db_engine_reset")
async def test_honest_gaps_never_fabricated() -> None:
    """appointments-booked / response-time / top-knowledge-sources / conversion-by-
    source/service must be null/empty + a reason string, NEVER a fabricated 0/[]."""
    admin = uuid.uuid4()
    await _seed(admin)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=0,
        )

    assert agg.appointments_booked is None
    assert agg.appointments_booked_gap is not None

    assert agg.response_time_p50_seconds is None
    assert agg.response_time_p95_seconds is None
    assert agg.response_time_gap is not None

    assert agg.top_knowledge_sources == []
    assert agg.top_knowledge_sources_gap is not None

    assert agg.conversion_by_source_gap is not None
    assert agg.conversion_by_service_gap is not None

    # Owner-language empty states only: never leak internal engineering notes
    # (file paths, Arc/workstream names, "code path" language, etc.) onto the
    # wire. This is served verbatim in AnalyticsOverviewOut.*_note.
    leaked_terms = (
        "arc ",
        "app/",
        ".py",
        "code path",
        "workstream",
        "build_brief",
        "fabricat",
        "module docstring",
    )
    all_notes = [
        agg.appointments_booked_gap.reason,
        agg.response_time_gap.reason,
        agg.top_knowledge_sources_gap.reason,
        agg.conversion_by_source_gap.reason,
        agg.conversion_by_service_gap.reason,
    ]
    for note in all_notes:
        lowered = note.lower()
        for term in leaked_terms:
            assert term not in lowered, f"dev-note leak in owner-facing copy: {note!r}"


@pytest.mark.usefixtures("db_engine_reset")
@pytest.mark.parametrize("view", sorted(analytics_service.CSV_VIEWS))
async def test_render_csv_all_views_parse(view: str) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin)
    await _add_session(admin, iid, channel="widget", started_at=datetime.now(UTC))
    await _add_escalation(admin, iid, signal="cannot_answer")

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=1,
        )
        audit_rows = [] if view != "audit_log" else []
        text = analytics_service.render_csv(view, agg, audit_rows)

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) >= 1  # at least the header row
    assert text.endswith("\r\n")


def test_render_csv_unknown_view_raises() -> None:
    agg = analytics_service.AnalyticsAggregate(
        conversations_this_period=0,
        conversations_total=0,
        leads_this_period=0,
        escalations_by_signal=[],
        channel_mix=[],
        busiest_times=[[0] * 24 for _ in range(7)],
        appointments_booked=None,
        appointments_booked_gap=analytics_service.TOOL_CALL_GAP,
        response_time_p50_seconds=None,
        response_time_p95_seconds=None,
        response_time_gap=analytics_service.RESPONSE_TIME_GAP,
        top_knowledge_sources=[],
        top_knowledge_sources_gap=analytics_service.RETRIEVAL_GAP,
    )
    with pytest.raises(ValueError):
        analytics_service.render_csv("not_a_real_view", agg)


def test_csv_escape_handles_commas_quotes_newlines() -> None:
    text = analytics_service._rows_to_csv(
        ["a", "b"], [["has,comma", 'has"quote'], ["has\nnewline", 1]]
    )
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert rows[1] == ["has,comma", 'has"quote']
    assert rows[2] == ["has\nnewline", "1"]
