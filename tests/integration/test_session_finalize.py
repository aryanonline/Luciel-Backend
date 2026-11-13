"""Session finalization sweep — summarization actually fires at session end (§3.4.7/§3.4.8).

Before app/runtime/session_close.py existed, session end was purely implicit: nothing
ever stamped an end or wrote a summary, so cross-session memory (§3.4.6) had an empty
store to read from. These tests pin the sweep's contract: per-channel windows honored,
``ended_at`` at the honest boundary, summary exactly once, idempotent under re-runs,
and zero-turn sessions sealed without a noise summary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as ConvSession
from app.models.conversation import SessionSummary, Transcript
from app.models.instance import Instance, LucielState
from app.runtime.session_close import finalize_due_sessions, finalize_session
from sqlalchemy import func, select

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


async def _seed_instance(admin_id: uuid.UUID, instance_id: uuid.UUID) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Instance(
                id=instance_id, admin_id=admin_id, name="Acme Patios", state=LucielState.active
            )
        )
        await s.flush()


async def _seed_session(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    channel: str,
    idle: timedelta,
    turns: int = 2,
) -> uuid.UUID:
    sid = uuid.uuid4()
    last = NOW - idle
    async with tenant_session(admin_id) as s:
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin_id,
                instance_id=instance_id,
                channel=channel,
                mode="ai",
                counted=False,
                started_at=last - timedelta(minutes=5),
                last_message_at=last,
            )
        )
        for i in range(turns):
            s.add(
                Transcript(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=sid,
                    role="lead" if i % 2 == 0 else "luciel",
                    text=f"turn {i}: do you deliver on Sundays?",
                    at=last - timedelta(minutes=turns - i),
                )
            )
        await s.flush()
    return sid


async def _summary_count(admin_id: uuid.UUID, session_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return (
            await s.scalar(
                select(func.count())
                .select_from(SessionSummary)
                .where(SessionSummary.admin_id == admin_id, SessionSummary.session_id == session_id)
            )
        ) or 0


async def _session(admin_id: uuid.UUID, session_id: uuid.UUID) -> ConvSession:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(ConvSession).where(
                    ConvSession.admin_id == admin_id, ConvSession.id == session_id
                )
            )
        ).scalar_one()


async def _run_sweep(admin_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return await finalize_due_sessions(s, admin_id=admin_id, now=NOW)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sessions_past_their_channel_window_are_sealed_and_summarized():
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    # Past-window on each channel class: widget 30m, sms 4h, email 24h (§3.4.8).
    widget_sid = await _seed_session(admin_id, iid, channel="widget", idle=timedelta(minutes=45))
    sms_sid = await _seed_session(admin_id, iid, channel="sms", idle=timedelta(hours=5))
    email_sid = await _seed_session(admin_id, iid, channel="email", idle=timedelta(hours=30))

    finalized = await _run_sweep(admin_id)
    assert finalized == 3

    for sid, timeout in (
        (widget_sid, timedelta(minutes=30)),
        (sms_sid, timedelta(hours=4)),
        (email_sid, timedelta(hours=24)),
    ):
        sess = await _session(admin_id, sid)
        assert sess.ended_at is not None
        # ended_at is the honest §3.4.8 boundary, not the sweep's wall-clock moment.
        assert sess.ended_at == sess.last_message_at + timeout
        assert sess.summary  # dashboard one-liner stamped
        assert await _summary_count(admin_id, sid) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sessions_inside_their_window_are_left_alone():
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    # 2 idle hours: past the widget window but INSIDE sms's 4h and email's 24h.
    sms_sid = await _seed_session(admin_id, iid, channel="sms", idle=timedelta(hours=2))
    email_sid = await _seed_session(admin_id, iid, channel="email", idle=timedelta(hours=2))

    finalized = await _run_sweep(admin_id)
    assert finalized == 0
    assert (await _session(admin_id, sms_sid)).ended_at is None
    assert (await _session(admin_id, email_sid)).ended_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_is_idempotent_under_rerun():
    """At-least-once delivery: a second run (retry, overlapping schedule) must be a
    no-op — one summary per session, ended_at unchanged."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    sid = await _seed_session(admin_id, iid, channel="sms", idle=timedelta(hours=6))

    assert await _run_sweep(admin_id) == 1
    first_ended_at = (await _session(admin_id, sid)).ended_at
    assert await _run_sweep(admin_id) == 0
    assert (await _session(admin_id, sid)).ended_at == first_ended_at
    assert await _summary_count(admin_id, sid) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_zero_turn_sessions_are_sealed_without_a_noise_summary():
    """A session with no transcript (e.g. an at-cap intake reply, §3.4.1b) still gets
    ended_at — it must never be resumable — but no summary row: an empty conversation
    has nothing to remember."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    sid = await _seed_session(admin_id, iid, channel="sms", idle=timedelta(hours=6), turns=0)

    assert await _run_sweep(admin_id) == 1
    sess = await _session(admin_id, sid)
    assert sess.ended_at is not None
    assert sess.summary is None
    assert await _summary_count(admin_id, sid) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_combined_channel_sessions_finalize_on_the_messaging_window():
    """Sessions opened before the §3.1.2 split carry channel='instagram_messenger';
    they still close on the 4-hour asynchronous-messaging window."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    sid = await _seed_session(admin_id, iid, channel="instagram_messenger", idle=timedelta(hours=5))

    assert await _run_sweep(admin_id) == 1
    assert (await _session(admin_id, sid)).ended_at is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_explicit_end_signal_finalizes_now_and_is_idempotent():
    """The widget close beacon path (§3.4.8 trigger (b)): ends at the signal moment,
    summarizes once, and a repeat signal is a no-op."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    sid = await _seed_session(admin_id, iid, channel="widget", idle=timedelta(minutes=1))

    async with tenant_session(admin_id) as s:
        assert (
            await finalize_session(
                s,
                admin_id=admin_id,
                instance_id=iid,
                channel="widget",
                session_id=sid,
                now=NOW,
            )
            is True
        )
    sess = await _session(admin_id, sid)
    assert sess.ended_at == NOW
    assert await _summary_count(admin_id, sid) == 1

    async with tenant_session(admin_id) as s:
        assert (
            await finalize_session(
                s,
                admin_id=admin_id,
                instance_id=iid,
                channel="widget",
                session_id=sid,
                now=NOW + timedelta(minutes=1),
            )
            is False
        )
    assert await _summary_count(admin_id, sid) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_explicit_end_is_scoped_to_instance_and_channel():
    """A caller can only end a session on ITS instance + channel — the beacon endpoint
    passes channel='widget', so an SMS session is untouchable through it."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    sid = await _seed_session(admin_id, iid, channel="sms", idle=timedelta(minutes=1))

    async with tenant_session(admin_id) as s:
        assert (
            await finalize_session(
                s,
                admin_id=admin_id,
                instance_id=iid,
                channel="widget",
                session_id=sid,
                now=NOW,
            )
            is False
        )
    assert (await _session(admin_id, sid)).ended_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_voice_session_missed_by_the_status_callback_is_swept():
    """The 30-minute voice window is the BACKSTOP: a call normally ends at hangup
    via the Twilio status callback, but a session whose callback never arrived
    (network loss, misconfigured number) must still be sealed by the sweep."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin_id, iid)
    voice_sid = await _seed_session(admin_id, iid, channel="voice", idle=timedelta(minutes=45))

    finalized = await _run_sweep(admin_id)
    assert finalized == 1
    sess = await _session(admin_id, voice_sid)
    assert sess.ended_at == sess.last_message_at + timedelta(minutes=30)
    assert sess.summary
