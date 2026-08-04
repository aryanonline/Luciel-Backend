"""Analytics & Reporting aggregate queries (DOCTRINE ANCHOR — Architecture §3.9).

Read-only, ``admin_id``-scoped aggregates over EXISTING tenant stores only —
sessions, leads, escalation_events, budget counters, admin_audit_log. No new
write path; no new PII; every query runs inside the caller's ``tenant_session``
so RLS (§3.7.2b) scopes every aggregate to the caller's tenant.

Honest-gap policy (Arch §3.9 data-sources table): §3.9 documents two data
sources that do not exist yet as durable stores in this codebase today:

  * Trace store ``tool_calls``       — appointments-booked would read this.
  * Trace store ``retrieval_results`` — top-knowledge-sources would read this.

``app/tools/broker.py`` only keeps an in-memory, per-request ``traces`` list
(explicitly flagged there: "the trace store §5.4 persists these in Arc 14" —
not yet true) and ``app/runtime/knowledge_retrieval.py`` never persists a
retrieval-frequency log. Both are OWNED by other workstreams (tools/broker,
knowledge) per BUILD_BRIEF.md file ownership, so this module cannot add the
missing persistence itself without crossing that boundary.

Rather than fabricate a plausible-looking number, both metrics are returned as
an honest "not yet available" gap (``available=False``, no count), exactly the
treatment the task spec mandates for escalation→response-time percentiles
(no ack-timestamp store exists either — ``escalation_acked`` is a defined
audit event type in app/audit/events.py but is never emitted anywhere in the
codebase today, so no ack timestamp exists to compute p50/p95 from).

Similarly, Vision §7 "conversion by source / channel / service" requires an
admin-marked lead OUTCOME ("converted" / "lost" / "in-progress"); ``Lead`` only
has ``state: active|archived`` (Arc 9 placeholder, not an outcome — the field
Vision §7 describes doesn't exist and ``app/models/lead.py`` is CORE LOOP-owned,
do-not-edit). There is also no persisted "service type" dimension anywhere.
What over IS real and computable: conversion proxy by CHANNEL as the
lead-capture rate (leads captured / conversations) per channel — grounded in
Session.channel + Session.resolved_lead_id, both real columns. That proxy is
surfaced as ``conversion_by_channel``; by-source and by-service are surfaced
as an honest empty ``not_available`` list (no source/service dimension is
persisted anywhere to group by).

Busiest-times and escalations ARE fully real: Session.started_at (hour-of-day x
day-of-week) and EscalationEvent rows all exist and are queried for real below.

Conversation-counting semantics (single source of truth, harmony fix 2026-08-04):
``conversation == billed session``, i.e. a row in ``counted_sessions`` (the same
fire-once table ``app/runtime/budget.py::count_session_once`` writes and the
Billing page's ``conversationsThisPeriod`` reads via ``metering.build_budget``).
Before this fix, ``conversations_total``/``channel_mix``/``conversion_by_channel``
counted raw ``sessions`` rows (every session ever opened, including sessions that
never reached a first model call and so were never billed) while
``conversations_this_period`` counted ``counted_sessions`` rows for the current
billing period only — four surfaces (Overview, Billing, Analytics, the per-channel
breakdown) could each show a different number for what a business owner reads as
one fact: "how many conversations have I had". All four now derive from
``counted_sessions``:
  * ``conversations_this_period`` — counted_sessions rows in the current billing
    period (unchanged; this was already correct — sourced from the budget counter).
  * ``conversations_total`` — counted_sessions rows, all billing periods (all-time
    billed conversations), NOT a raw session count.
  * ``channel_mix`` / ``conversion_by_channel`` — sessions joined to counted_sessions
    (i.e. restricted to billed sessions), scoped to the current billing period, so
    the per-channel breakdown sums back to ``conversations_this_period`` exactly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AdminAuditLog
from app.models.budget import CountedSession
from app.models.conversation import EscalationEvent, Session
from app.models.lead import Lead

# Vision §7 / Arch §3.9: heatmap is [dayOfWeek 0-6][hourOfDay 0-23] -> count.
_DAYS = 7
_HOURS = 24


@dataclass
class EscalationSignalCount:
    signal: str
    count: int


@dataclass
class ChannelFraction:
    channel: str
    fraction: float


@dataclass
class ChannelConversion:
    channel: str
    conversations: int
    leads: int
    conversion_rate: float  # leads / conversations, 0.0 when no conversations


@dataclass
class NotAvailable:
    """Honest gap marker (Arch §3.9 — aggregates only, never fabricated)."""

    reason: str


@dataclass
class AnalyticsAggregate:
    conversations_this_period: int
    conversations_total: int
    leads_this_period: int
    escalations_by_signal: list[EscalationSignalCount]
    channel_mix: list[ChannelFraction]
    busiest_times: list[list[int]]  # [day][hour] -> count, real data over all sessions
    # Honest gaps — no durable store exists yet (see module docstring).
    appointments_booked: int | None
    appointments_booked_gap: NotAvailable | None
    response_time_p50_seconds: float | None
    response_time_p95_seconds: float | None
    response_time_gap: NotAvailable | None
    top_knowledge_sources: list[dict[str, object]]
    top_knowledge_sources_gap: NotAvailable | None
    conversion_by_channel: list[ChannelConversion] = field(default_factory=list)
    conversion_by_source_gap: NotAvailable | None = None
    conversion_by_service_gap: NotAvailable | None = None


# Owner-facing copy only. These strings are served verbatim to the dashboard
# (AnalyticsOverviewOut.*_note) — they must read like a product empty state,
# never like an engineering note (no file paths, arc/workstream names, or
# "code path" language). See module docstring above for the internal reasons;
# keep any internals-facing detail in comments/docstrings, not in these
# strings.
TOOL_CALL_GAP = NotAvailable("Tool activity isn't tracked yet — coming soon.")
RETRIEVAL_GAP = NotAvailable("Knowledge source usage isn't tracked yet — coming soon.")
RESPONSE_TIME_GAP = NotAvailable("Response time tracking isn't available yet — coming soon.")
CONVERSION_SOURCE_GAP = NotAvailable("Conversion by source isn't available yet — coming soon.")
CONVERSION_SERVICE_GAP = NotAvailable("Conversion by service isn't available yet — coming soon.")


async def _escalations_by_signal(
    s: AsyncSession, *, period_start: date
) -> list[EscalationSignalCount]:
    rows = (
        await s.execute(
            select(EscalationEvent.signal, func.count())
            .where(EscalationEvent.fired_at >= period_start)
            .group_by(EscalationEvent.signal)
        )
    ).all()
    return [EscalationSignalCount(signal=sig, count=cnt) for sig, cnt in rows]


async def _channel_mix(s: AsyncSession, *, period_start: date) -> list[ChannelFraction]:
    """Channel mix over BILLED conversations this period (see module docstring —
    single conversation-counting source of truth). Joins sessions to
    counted_sessions so a session that opened but never reached a first model
    call (never billed) is not counted here either."""
    rows = (
        await s.execute(
            select(Session.channel, func.count())
            .select_from(Session)
            .join(
                CountedSession,
                (CountedSession.session_id == Session.id)
                & (CountedSession.billing_period_start == period_start),
            )
            .group_by(Session.channel)
        )
    ).all()
    total = sum(cnt for _, cnt in rows) or 0
    return [
        ChannelFraction(channel=ch, fraction=(cnt / total) if total else 0.0) for ch, cnt in rows
    ]


async def _busiest_times(s: AsyncSession) -> list[list[int]]:
    """Conversation volume heatmap by hour-of-day x day-of-week (Arch §3.9).

    Real data over Session.started_at. Aggregated in Python (not SQL EXTRACT) so
    the query is portable across the Postgres prod engine and any test engine,
    and so the day-of-week convention (Python's Monday=0) is applied identically
    regardless of backend-specific date function semantics.
    """
    rows = (await s.execute(select(Session.started_at))).scalars().all()
    grid = [[0] * _HOURS for _ in range(_DAYS)]
    for started_at in rows:
        if started_at is None:
            continue
        grid[started_at.weekday()][started_at.hour] += 1
    return grid


async def _top_knowledge_sources() -> tuple[list[dict[str, object]], NotAvailable]:
    # See module docstring / RETRIEVAL_GAP — no retrieval-frequency log exists.
    return [], RETRIEVAL_GAP


async def _appointments_booked() -> tuple[int | None, NotAvailable]:
    # See module docstring / TOOL_CALL_GAP — no durable tool-call store exists.
    return None, TOOL_CALL_GAP


async def _response_time_percentiles() -> tuple[float | None, float | None, NotAvailable]:
    # See module docstring / RESPONSE_TIME_GAP — no ack-timestamp store exists.
    return None, None, RESPONSE_TIME_GAP


async def _conversion_by_channel(s: AsyncSession, *, period_start: date) -> list[ChannelConversion]:
    """Lead-capture-rate proxy for 'conversion by channel' (Vision §7).

    Grounded in real columns: Session.channel and Session.resolved_lead_id (set
    when a session resolves to a captured lead). This is the honest metric that
    IS computable today — a true admin-marked-outcome conversion rate is not
    (see conversion_by_source/service gaps + module docstring).

    ``conversations`` here means BILLED conversations this period (see module
    docstring): joined to counted_sessions so this sums back to exactly
    ``conversations_this_period``, matching Billing/Overview/Analytics.
    """
    rows = (
        await s.execute(
            select(
                Session.channel,
                func.count(),
                func.count(Session.resolved_lead_id),
            )
            .select_from(Session)
            .join(
                CountedSession,
                (CountedSession.session_id == Session.id)
                & (CountedSession.billing_period_start == period_start),
            )
            .group_by(Session.channel)
        )
    ).all()
    return [
        ChannelConversion(
            channel=ch,
            conversations=conv,
            leads=leads,
            conversion_rate=(leads / conv) if conv else 0.0,
        )
        for ch, conv, leads in rows
    ]


async def build_overview(
    s: AsyncSession,
    *,
    admin_id: uuid.UUID,
    period_start: date,
    conversations_this_period: int,
) -> AnalyticsAggregate:
    """Assemble the full analytics overview aggregate (Arch §3.9).

    ``conversations_this_period`` is passed in by the caller because it comes
    from the budget counter — the single source of truth for that count
    (§3.4.1b) — NOT recomputed here from sessions.
    """
    # conversations_total = all-time BILLED conversations (counted_sessions rows),
    # not a raw session count — see module docstring (single conversation-counting
    # source of truth).
    conversations_total = (
        await s.execute(
            select(func.count())
            .select_from(CountedSession)
            .where(CountedSession.admin_id == admin_id)
        )
    ).scalar_one()
    leads_this_period = (
        await s.execute(
            select(func.count()).select_from(Lead).where(Lead.created_at >= period_start)
        )
    ).scalar_one()

    escalations_by_signal = await _escalations_by_signal(s, period_start=period_start)
    channel_mix = await _channel_mix(s, period_start=period_start)
    busiest_times = await _busiest_times(s)
    top_sources, sources_gap = await _top_knowledge_sources()
    appointments, appointments_gap = await _appointments_booked()
    p50, p95, response_gap = await _response_time_percentiles()
    conversion_by_channel = await _conversion_by_channel(s, period_start=period_start)

    return AnalyticsAggregate(
        conversations_this_period=conversations_this_period,
        conversations_total=conversations_total,
        leads_this_period=leads_this_period,
        escalations_by_signal=escalations_by_signal,
        channel_mix=channel_mix,
        busiest_times=busiest_times,
        appointments_booked=appointments,
        appointments_booked_gap=appointments_gap,
        response_time_p50_seconds=p50,
        response_time_p95_seconds=p95,
        response_time_gap=response_gap,
        top_knowledge_sources=top_sources,
        top_knowledge_sources_gap=sources_gap,
        conversion_by_channel=conversion_by_channel,
        conversion_by_source_gap=CONVERSION_SOURCE_GAP,
        conversion_by_service_gap=CONVERSION_SERVICE_GAP,
    )


# --- CSV export (Vision §7: "exportable reports — CSV download of any analytics
# view for the selected period") ------------------------------------------------ #

CSV_VIEWS: frozenset[str] = frozenset(
    {
        "overview",
        "escalations_by_signal",
        "channel_mix",
        "busiest_times",
        "conversion_by_channel",
        "audit_log",
    }
)


def _csv_escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(c in text for c in (",", '"', "\n")):
        text = '"' + text.replace('"', '""') + '"'
    return text


def _rows_to_csv(header: list[str], rows: list[list[object]]) -> str:
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(_csv_escape(v) for v in row))
    return "\r\n".join(lines) + "\r\n"


def render_csv(
    view: str, agg: AnalyticsAggregate, audit_rows: list[AdminAuditLog] | None = None
) -> str:
    """Render one named analytics view as CSV text (Vision §7 CSV export).

    ``view`` must be one of ``CSV_VIEWS``; callers (the endpoint) validate that
    and return 404 for an unknown view before calling this."""
    if view == "overview":
        header = ["metric", "value"]
        rows: list[list[object]] = [
            ["conversations_this_period", agg.conversations_this_period],
            ["conversations_total", agg.conversations_total],
            ["leads_this_period", agg.leads_this_period],
            [
                "appointments_booked",
                agg.appointments_booked if agg.appointments_booked is not None else "not_available",
            ],
            [
                "response_time_p50_seconds",
                agg.response_time_p50_seconds
                if agg.response_time_p50_seconds is not None
                else "not_available",
            ],
            [
                "response_time_p95_seconds",
                agg.response_time_p95_seconds
                if agg.response_time_p95_seconds is not None
                else "not_available",
            ],
        ]
        return _rows_to_csv(header, rows)

    if view == "escalations_by_signal":
        return _rows_to_csv(
            ["signal", "count"], [[e.signal, e.count] for e in agg.escalations_by_signal]
        )

    if view == "channel_mix":
        return _rows_to_csv(
            ["channel", "fraction"], [[c.channel, c.fraction] for c in agg.channel_mix]
        )

    if view == "busiest_times":
        header = ["day_of_week", *[f"hour_{h}" for h in range(_HOURS)]]
        rows = [[day, *agg.busiest_times[day]] for day in range(_DAYS)]
        return _rows_to_csv(header, rows)

    if view == "conversion_by_channel":
        return _rows_to_csv(
            ["channel", "conversations", "leads", "conversion_rate"],
            [
                [c.channel, c.conversations, c.leads, c.conversion_rate]
                for c in agg.conversion_by_channel
            ],
        )

    if view == "audit_log":
        audit_rows = audit_rows or []
        return _rows_to_csv(
            ["event_id", "event_type", "at", "actor_user_id"],
            [
                [str(r.event_id), r.event_type, r.created_at.isoformat(), r.actor_user_id]
                for r in audit_rows
            ],
        )

    raise ValueError(f"Unknown analytics CSV view: {view!r}")
