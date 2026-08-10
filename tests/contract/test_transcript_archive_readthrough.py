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
from app.lifecycle.transcript_archive import archive_aged_transcripts
from app.lifecycle.transcript_store import LocalTranscriptStore
from app.models.conversation import Session as ConvSession
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalTranscriptStore.reset()
    yield
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
