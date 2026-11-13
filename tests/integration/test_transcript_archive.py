"""Transcript hot→cold archival + 1-year reconciliation (Commitment #38, §3.4.10).

The ratified model: transcripts MOVE to S3 cold storage at 90 days (hot Postgres
plateaus), are still served on demand (dashboard + export never go blank), and the
retention sweep deletes the cold object at the 1-year boundary (cold storage
plateaus too). The hard-delete cascade wipes a deleted tenant's whole archive
prefix. Each property is pinned here against the Local store seam — the exact code
path live mode runs, with only the S3 client swapped out.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.jobs import sweeps
from app.lifecycle import transcript_store as store_mod
from app.lifecycle.transcript_archive import archive_aged_transcripts
from app.lifecycle.transcript_store import LocalTranscriptStore, transcript_prefix_for
from app.models.conversation import Session as ConvSession
from app.models.conversation import Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import func, select

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


class DurableLocalStore(LocalTranscriptStore):
    """Stands in for the real S3 store: same behavior, but flagged DURABLE.

    The archive sweep refuses to delete hot rows into a store that cannot outlive the
    process (see ``LocalTranscriptStore.durable``), so the tests that exercise the
    MOVE must bind a store that claims what S3 provides. The refusal itself is
    covered by ``test_the_sweep_refuses_to_move_into_a_non_durable_store``.
    """

    durable = True


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalTranscriptStore.reset()
    store_mod.set_transcript_store(DurableLocalStore())
    yield
    store_mod.set_transcript_store(None)
    LocalTranscriptStore.reset()


async def _seed_tenant(admin_id: uuid.UUID, instance_id: uuid.UUID) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(Instance(id=instance_id, admin_id=admin_id, name="Acme", state=LucielState.active))
        await s.flush()


async def _seed_ended_session(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    age: timedelta,
    turns: int = 4,
    created_at: datetime | None = None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    last = NOW - age
    async with tenant_session(admin_id) as s:
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin_id,
                instance_id=instance_id,
                channel="sms",
                mode="ai",
                counted=False,
                started_at=last - timedelta(minutes=10),
                last_message_at=last,
                ended_at=last + timedelta(hours=4),
                **({} if created_at is None else {"created_at": created_at}),
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
                    grounding_score=0.9 if i % 2 else None,
                )
            )
        await s.flush()
    return sid


async def _hot_rows(admin_id: uuid.UUID, sid: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return (
            await s.scalar(
                select(func.count())
                .select_from(Transcript)
                .where(Transcript.admin_id == admin_id, Transcript.session_id == sid)
            )
        ) or 0


async def _session(admin_id: uuid.UUID, sid: uuid.UUID) -> ConvSession:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(ConvSession).where(ConvSession.admin_id == admin_id, ConvSession.id == sid)
            )
        ).scalar_one()


async def _archive(admin_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return await archive_aged_transcripts(s, admin_id=admin_id, now=NOW)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_aged_sessions_move_to_cold_storage_and_hot_rows_are_freed():
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    aged = await _seed_ended_session(admin_id, iid, age=timedelta(days=120))
    fresh = await _seed_ended_session(admin_id, iid, age=timedelta(days=10))

    assert await _archive(admin_id) == 1

    # The aged session's rows MOVED: object present, hot rows gone, markers set.
    sess = await _session(admin_id, aged)
    assert sess.archive_object_key and str(aged) in sess.archive_object_key
    assert sess.transcripts_archived_at is not None
    assert await _hot_rows(admin_id, aged) == 0
    data = await LocalTranscriptStore().get(key=sess.archive_object_key)
    assert data is not None and b"do you deliver on Sundays?" in data
    # The fresh session is untouched — still hot.
    assert await _hot_rows(admin_id, fresh) == 4
    assert (await _session(admin_id, fresh)).archive_object_key is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_archive_sweep_is_idempotent_under_rerun():
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    await _seed_ended_session(admin_id, iid, age=timedelta(days=120))

    assert await _archive(admin_id) == 1
    keys_after_first = LocalTranscriptStore.keys()
    assert await _archive(admin_id) == 0
    assert LocalTranscriptStore.keys() == keys_after_first


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_live_session_is_never_archived():
    """Only ENDED sessions archive: a live transcript forked across two stores would
    corrupt the conversation. (In practice a 90-day-idle session is long past every
    inactivity window, but the gate guards the invariant.)"""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    sid = await _seed_ended_session(admin_id, iid, age=timedelta(days=120))
    async with tenant_session(admin_id) as s:
        sess = (await s.execute(select(ConvSession).where(ConvSession.id == sid))).scalar_one()
        sess.ended_at = None  # simulate a session finalization never sealed

    assert await _archive(admin_id) == 0
    assert await _hot_rows(admin_id, sid) == 4


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_refuses_to_move_into_a_non_durable_store():
    """FOUND ON DEV — the failure this guard exists for.

    In mock mode the bound store is an in-process dict. The sweep runs as its OWN
    one-shot process (``python -m app.jobs transcript_archive``), so it deleted the
    hot rows, wrote the "archive" into memory that died with the process, and left
    the session pointing at a key that resolved to nothing: the customer's
    conversation was silently, permanently gone. A "move" is only a move when the
    destination outlives the mover — otherwise the sweep must do nothing at all.

    The in-process test suite could not see this: it archived and read back inside
    one process, where the dict is still alive.
    """
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    sid = await _seed_ended_session(admin_id, iid, age=timedelta(days=120))

    # The DEFAULT mock-mode store (non-durable), not the DurableLocalStore double.
    store_mod.set_transcript_store(LocalTranscriptStore())
    try:
        assert await _archive(admin_id) == 0, (
            "the sweep moved data into a store that cannot keep it"
        )
    finally:
        store_mod.set_transcript_store(DurableLocalStore())

    # Everything stays exactly where it is safe: hot, unmarked, readable.
    assert await _hot_rows(admin_id, sid) == 4
    sess = await _session(admin_id, sid)
    assert sess.archive_object_key is None
    assert sess.transcripts_archived_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_put_failure_leaves_everything_hot_for_the_next_run():
    from app.lifecycle import transcript_store as store_mod

    class ExplodingStore(DurableLocalStore):
        """A durable store that is momentarily unreachable — the put fails."""

        async def put(self, *, key: str, data: bytes) -> None:
            raise RuntimeError("s3 down")

    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    sid = await _seed_ended_session(admin_id, iid, age=timedelta(days=120))

    store_mod.set_transcript_store(ExplodingStore())
    try:
        with pytest.raises(RuntimeError):
            await _archive(admin_id)
    finally:
        store_mod.set_transcript_store(DurableLocalStore())

    # Nothing moved, nothing stamped — the next healthy run picks it up whole.
    assert await _hot_rows(admin_id, sid) == 4
    sess = await _session(admin_id, sid)
    assert sess.archive_object_key is None and sess.transcripts_archived_at is None
    assert await _archive(admin_id) == 1  # recovery run succeeds


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_bundle_includes_archived_transcripts():
    """'Download all my data' must include conversations past the 90-day boundary."""
    import io
    import json
    import zipfile

    from app.lifecycle.export_bundle import build_export_zip

    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    sid = await _seed_ended_session(admin_id, iid, age=timedelta(days=120))
    assert await _archive(admin_id) == 1

    async with tenant_session(admin_id) as s:
        bundle = await build_export_zip(s, admin_id=admin_id)
    with zipfile.ZipFile(io.BytesIO(bundle)) as z:
        conv = json.loads(z.read(f"conversations/{sid}.json"))
    assert len(conv["messages"]) == 4
    assert any("do you deliver" in m["text"] for m in conv["messages"])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retention_sweep_deletes_the_cold_object_at_the_1_year_boundary():
    """The app sweep — not the bucket lifecycle backstop — is the authoritative
    deleter: rows AND object go together at 365 days, audited."""
    from app.lifecycle.retention import expire_aged_data

    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    old = NOW - timedelta(days=400)
    sid = await _seed_ended_session(admin_id, iid, age=timedelta(days=400), created_at=old)
    assert await _archive(admin_id) == 1
    key = (await _session(admin_id, sid)).archive_object_key
    assert key and await LocalTranscriptStore().get(key=key) is not None

    async with tenant_session(admin_id) as s:
        counts = await expire_aged_data(s, admin_id=admin_id, now=NOW)

    assert counts["transcript_archive_object"] == 1
    assert counts["session"] >= 1
    assert await LocalTranscriptStore().get(key=key) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cascade_wipes_the_tenants_archive_prefix_and_no_one_elses():
    from app.lifecycle.cascade import hard_delete_cascade

    admin_a, ia = uuid.uuid4(), uuid.uuid4()
    admin_b, ib = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_a, ia)
    await _seed_tenant(admin_b, ib)
    await _seed_ended_session(admin_a, ia, age=timedelta(days=120))
    await _seed_ended_session(admin_b, ib, age=timedelta(days=120))
    assert await _archive(admin_a) == 1
    assert await _archive(admin_b) == 1

    async with tenant_session(admin_a) as s:
        await hard_delete_cascade(s, admin_a)

    remaining = LocalTranscriptStore.keys()
    assert all(not k.startswith(transcript_prefix_for(admin_a)) for k in remaining)
    # The neighbour tenant's archive survives — the prefix sweep is tenant-scoped.
    assert any(k.startswith(transcript_prefix_for(admin_b)) for k in remaining)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_entrypoint_discovers_and_archives_through_the_job_path():
    """End-to-end through python -m app.jobs transcript_archive: discovery (0034
    candidates branch) → per-tenant lock → archive."""
    admin_id, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, iid)
    await _seed_ended_session(admin_id, iid, age=timedelta(days=120))

    acted = await sweeps.run_transcript_archive(now=NOW)
    assert str(admin_id) in acted
