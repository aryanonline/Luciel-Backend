"""Dashboard read-through for archived transcripts (Commitment #38, §3.4.10).

A conversation past the 90-day hot boundary has no Transcript rows — its transcript
lives in S3 cold storage. The messages endpoint must serve it identically from the
archive, so the owner's conversation view never goes blank the day the rows moved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.lifecycle import transcript_store as store_mod
from app.lifecycle.transcript_archive import archive_aged_transcripts
from app.lifecycle.transcript_store import LocalTranscriptStore, TranscriptStoreUnavailable
from app.models.conversation import Session as ConvSession
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


class DurableLocalStore(LocalTranscriptStore):
    """Stands in for S3: same behavior, flagged durable so the archive sweep will
    actually move (the sweep refuses non-durable stores — see the archive tests)."""

    durable = True


class OutageStore(DurableLocalStore):
    """S3 during an outage: the objects are all still there, the store cannot answer."""

    async def get(self, *, key: str) -> bytes | None:
        raise TranscriptStoreUnavailable("ClientError:SlowDown")


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalTranscriptStore.reset()
    store_mod.set_transcript_store(DurableLocalStore())
    yield
    store_mod.set_transcript_store(None)
    LocalTranscriptStore.reset()


async def _login(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"arch-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dashboard_serves_an_archived_conversation_identically(client):
    admin_id = await _login(client)
    iid, sid = uuid.uuid4(), uuid.uuid4()
    last = NOW - timedelta(days=120)
    async with tenant_session(admin_id) as s:
        s.add(Instance(id=iid, admin_id=admin_id, name="Acme", state=LucielState.active))
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin_id,
                instance_id=iid,
                channel="sms",
                mode="ai",
                counted=False,
                started_at=last - timedelta(minutes=10),
                last_message_at=last,
                ended_at=last + timedelta(hours=4),
            )
        )
        for i in range(4):
            s.add(
                Transcript(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=iid,
                    session_id=sid,
                    role="lead" if i % 2 == 0 else "luciel",
                    text=f"turn {i}: do you deliver on Sundays?",
                    at=last - timedelta(minutes=4 - i),
                )
            )
        await s.flush()

    before = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert before.status_code == 200
    assert len(before.json()) == 4

    async with tenant_session(admin_id) as s:
        assert await archive_aged_transcripts(s, admin_id=admin_id, now=NOW) == 1

    after = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert after.status_code == 200
    # Identical content either side of the move — only the storage tier changed.
    assert [m["text"] for m in after.json()] == [m["text"] for m in before.json()]
    assert [m["role"] for m in after.json()] == [m["role"] for m in before.json()]


async def _seed_archived(client) -> tuple[uuid.UUID, uuid.UUID]:
    """An owner with one conversation already moved to cold storage."""
    admin_id = await _login(client)
    iid, sid = uuid.uuid4(), uuid.uuid4()
    last = NOW - timedelta(days=120)
    async with tenant_session(admin_id) as s:
        s.add(Instance(id=iid, admin_id=admin_id, name="Acme", state=LucielState.active))
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin_id,
                instance_id=iid,
                channel="sms",
                mode="ai",
                counted=False,
                started_at=last - timedelta(minutes=10),
                last_message_at=last,
                ended_at=last + timedelta(hours=4),
            )
        )
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=iid,
                session_id=sid,
                role="lead",
                text="do you deliver on Sundays?",
                at=last,
            )
        )
        await s.flush()
    async with tenant_session(admin_id) as s:
        assert await archive_aged_transcripts(s, admin_id=admin_id, now=NOW) == 1
    return admin_id, sid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unreadable_archive_is_reported_not_rendered_as_an_empty_conversation(client):
    """The two states look identical to the reader and mean opposite things.

    Both the dashboard and the export used to coalesce a cold-storage miss to an empty
    list, so a conversation we could not read back displayed as a conversation in which
    the customer had said nothing. That is a false claim about content, and the owner
    has no way to tell it from the truth.

    Round 7 WP-5, item 7: the store could not ANSWER (an outage, a refused credential)
    — the object is still there, so the copy says "could not be read right now" and
    may honestly promise that nothing has been lost.
    """
    _admin_id, sid = await _seed_archived(client)
    store_mod.set_transcript_store(OutageStore())

    response = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")

    assert response.status_code == 503
    body = response.json()["message"].lower()
    assert "long-term storage" in body
    assert "could not be read right now" in body
    assert "nothing has been lost" in body
    assert "no longer available" not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_absent_archive_object_is_reported_as_gone_not_as_a_hiccup(client):
    """The store ANSWERED: the key resolves to nothing (round 7 WP-5, item 7).

    This used to render the same 503 as an outage, with copy swearing "nothing has
    been lost; please try again shortly" — a promise the platform could not keep,
    because the object was gone and no retry would bring it back. The owner is told
    the true thing, and is not told to wait for a recovery that will not come.
    """
    _admin_id, sid = await _seed_archived(client)
    # The object is gone while the session still points at it.
    LocalTranscriptStore.reset()

    response = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")

    assert response.status_code == 404
    body = response.json()["message"].lower()
    assert "long-term storage" in body
    assert "no longer available" in body
    assert "nothing has been lost" not in body
    assert "try again" not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_read_through_raises_rather_than_returning_an_empty_transcript(client):
    """The seam itself distinguishes the two, so every caller inherits the honesty.

    The export bundle deliberately does not catch this: shipping a bundle that silently
    omits older conversations is worse than failing the export, because the customer was
    told the bundle is all of their data.
    """
    from app.lifecycle.transcript_archive import (
        ArchivedTranscriptAbsent,
        ArchivedTranscriptUnavailable,
        load_archived_transcripts,
    )

    admin_id, sid = await _seed_archived(client)
    LocalTranscriptStore.reset()

    async with tenant_session(admin_id) as s:
        # Absent is the SUBCLASS: every caller that refuses to fake an empty
        # conversation keeps refusing, and a caller that wants to say which can.
        with pytest.raises(ArchivedTranscriptAbsent):
            await load_archived_transcripts(s, admin_id=admin_id, session_id=sid)
        assert issubclass(ArchivedTranscriptAbsent, ArchivedTranscriptUnavailable)

        store_mod.set_transcript_store(OutageStore())
        with pytest.raises(ArchivedTranscriptUnavailable) as info:
            await load_archived_transcripts(s, admin_id=admin_id, session_id=sid)
        assert not isinstance(info.value, ArchivedTranscriptAbsent)
        assert isinstance(info.value.__cause__, TranscriptStoreUnavailable)

        # A session that was never archived is still an ordinary None, not an error.
        never_archived = await load_archived_transcripts(
            s, admin_id=admin_id, session_id=uuid.uuid4()
        )
        assert never_archived is None
