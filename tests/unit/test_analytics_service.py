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
from app.billing import metering
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Session
from app.models.instance import Instance, LucielState
from app.models.lead import Lead
from app.runtime import budget as budget_runtime
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


async def _count_session(admin: uuid.UUID, iid: uuid.UUID, session_id: uuid.UUID) -> None:
    """Mark a session as BILLED (insert into counted_sessions + bump the budget
    counter) via the real fire-once path — the same one the orchestrator calls on
    first model call (§3.4.1b). Tests must go through this, not a raw INSERT, so
    analytics stays wired to the actual conversation-counting semantics."""
    async with tenant_session(admin) as s:
        await budget_runtime.count_session_once(
            s, admin_id=admin, instance_id=iid, session_id=session_id
        )


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
    # Tuesday 09:00 UTC, twice, and Wednesday 14:00 once — all BILLED (the heatmap
    # counts billed conversations this period, like every other panel).
    tue_9am = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)  # 2026-07-07 is a Tuesday
    wed_2pm = datetime(2026, 7, 8, 14, 0, tzinfo=UTC)
    for channel, started_at in (("widget", tue_9am), ("widget", tue_9am), ("sms", wed_2pm)):
        sid = await _add_session(admin, iid, channel=channel, started_at=started_at)
        await _count_session(admin, iid, sid)

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
async def test_busiest_times_excludes_unbilled_sessions() -> None:
    """The heatmap shares the single conversation-counting source of truth: a session
    that opened but never billed (no first model call) must not appear — previously
    the heatmap read raw sessions all-time and disagreed with every other panel."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    tue_9am = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
    billed = await _add_session(admin, iid, channel="widget", started_at=tue_9am)
    await _count_session(admin, iid, billed)
    await _add_session(admin, iid, channel="widget", started_at=tue_9am)  # never billed

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=1,
        )

    assert sum(sum(row) for row in agg.busiest_times) == 1


@pytest.mark.usefixtures("db_engine_reset")
async def test_channel_mix_and_conversion_by_channel_are_real() -> None:
    """channel_mix / conversion_by_channel count BILLED conversations only (single
    conversation-counting source of truth, harmony fix item 2) — a session that
    opened but never had its first model call (never went through
    count_session_once) must NOT appear here, exactly like it doesn't count
    against the Billing/Overview conversationsThisPeriod number."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    lead = await _add_lead(admin, iid)
    now = datetime.now(UTC)
    widget_billed = await _add_session(
        admin, iid, channel="widget", started_at=now, resolved_lead_id=lead
    )
    widget_billed_2 = await _add_session(admin, iid, channel="widget", started_at=now)
    sms_billed = await _add_session(admin, iid, channel="sms", started_at=now)
    # Opened but never counted (e.g. abandoned before a first model call) — must
    # not be reflected in either metric, matching conversationsThisPeriod.
    await _add_session(admin, iid, channel="sms", started_at=now)

    await _count_session(admin, iid, widget_billed)
    await _count_session(admin, iid, widget_billed_2)
    await _count_session(admin, iid, sms_billed)

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
    assert conv_by_channel["sms"].conversations == 1, "the uncounted sms session must not appear"
    assert conv_by_channel["sms"].leads == 0
    assert conv_by_channel["sms"].conversion_rate == 0.0

    # The per-channel breakdown must sum back to exactly conversations_this_period
    # — this is the "channel table" surface from the walkthrough that showed 13
    # while Overview/Billing/Analytics showed 1/3/3.
    assert sum(c.conversations for c in agg.conversion_by_channel) == agg.conversations_this_period


@pytest.mark.usefixtures("db_engine_reset")
async def test_conversations_total_counts_billed_sessions_not_raw_sessions() -> None:
    """conversations_total must be the all-time BILLED count (counted_sessions),
    not a raw Session row count — harmony fix item 2 (Overview/Billing/Analytics/
    channel-table must agree on one semantics: conversation == billed session)."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    now = datetime.now(UTC)
    billed = await _add_session(admin, iid, channel="widget", started_at=now)
    await _add_session(admin, iid, channel="widget", started_at=now)  # never counted
    await _add_session(admin, iid, channel="widget", started_at=now)  # never counted
    await _count_session(admin, iid, billed)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=1,
        )

    assert agg.conversations_total == 1, (
        "conversations_total leaked raw (unbilled) session rows instead of counting "
        "billed conversations"
    )


@pytest.mark.usefixtures("db_engine_reset")
async def test_conversations_total_survives_the_invoice_paid_period_reset() -> None:
    """'All-time' must actually mean all time. The counted_sessions LEDGER is
    deliberately deleted by every invoice.paid reset (§3.4.1b) — so a total computed
    only from the ledger restarted at zero each billing period. The monotonic
    billing_accounts.lifetime_conversations (migration 0035) is what survives."""
    from app.models.billing import BillingAccount
    from app.models.budget import CountedSession
    from sqlalchemy import delete

    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        s.add(BillingAccount(admin_id=admin))
    now = datetime.now(UTC)
    for _ in range(3):
        sid = await _add_session(admin, iid, channel="widget", started_at=now)
        await _count_session(admin, iid, sid)

    # Simulate the invoice.paid ledger reset (app/billing/webhooks.py): every
    # counted_sessions row for the tenant is deleted when the period closes.
    async with tenant_session(admin) as s:
        await s.execute(delete(CountedSession).where(CountedSession.admin_id == admin))

    # A new period's conversation bills normally...
    sid = await _add_session(admin, iid, channel="widget", started_at=now)
    await _count_session(admin, iid, sid)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=1,
        )

    # ...and the all-time total still remembers the pre-reset three.
    assert agg.conversations_total == 4


@pytest.mark.usefixtures("db_engine_reset")
async def test_conversation_count_agrees_across_billing_overview_and_analytics() -> None:
    """Cross-surface harmony check (item 2): Billing's conversationsThisPeriod,
    Analytics' conversationsThisPeriod, and the per-channel breakdown total must
    all be the exact same number for the exact same admin/period — the walkthrough
    finding was four different surfaces disagreeing about one fact."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    now = datetime.now(UTC)
    for _ in range(3):
        sid = await _add_session(admin, iid, channel="widget", started_at=now)
        await _count_session(admin, iid, sid)

    async with tenant_session(admin) as s:
        budget = await metering.build_budget(s, admin_id=admin, instance_id=iid)
        # Billing surface number.
        billing_count = budget.conversations_this_period

        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=billing_count,
        )

    analytics_count = agg.conversations_this_period
    channel_table_total = sum(c.conversations for c in agg.conversion_by_channel)

    assert billing_count == 3
    assert analytics_count == billing_count
    assert channel_table_total == billing_count


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
    """Remaining gaps (top-knowledge-sources, conversion by source/service) must
    be null/empty + a reason string, NEVER a fabricated 0/[]. Appointments
    booked is REAL now (booking_events): its honest empty state IS 0 with no
    gap; reply time's honest empty state is nulls + the plain-language note."""
    admin = uuid.uuid4()
    await _seed(admin)

    async with tenant_session(admin) as s:
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=0,
        )

    assert agg.appointments_booked == 0  # a real zero from a real store
    assert agg.appointments_booked_gap is None

    assert agg.response_time_p50_seconds is None
    assert agg.response_time_p95_seconds is None
    assert agg.response_time_gap is not None  # empty state, not a build gap

    # Real since C14: empty is an honest empty (no retrieval traces / no leads
    # yet), never a permanent "coming soon" note.
    assert agg.top_knowledge_sources == []
    assert agg.top_knowledge_sources_gap is None

    assert agg.conversion_by_source == []
    assert agg.conversion_by_source_gap is None
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
        gap.reason
        for gap in (
            agg.appointments_booked_gap,  # None now — the store is real
            agg.response_time_gap,
            agg.top_knowledge_sources_gap,
            agg.conversion_by_source_gap,
            agg.conversion_by_service_gap,
        )
        if gap is not None
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
        appointments_booked=0,
        appointments_booked_gap=None,
        response_time_p50_seconds=None,
        response_time_p95_seconds=None,
        response_time_gap=analytics_service.RESPONSE_TIME_EMPTY,
        top_knowledge_sources=[],
        top_knowledge_sources_gap=None,
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
