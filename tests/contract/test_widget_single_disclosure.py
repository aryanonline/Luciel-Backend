"""Widget AI disclosure appears ONCE per conversation (§3.4.16 — audit round 3, C5).

Live defect: the bootstrap opening bubble carried the disclosure, nothing persisted
it, so the loop's first-message backstop saw no prior assistant turn and prepended
the same greeting again onto the first real reply — the visitor read it twice. The
fix persists the greeting as the new session's first `luciel` transcript row, which
both matches what the visitor actually saw and stands the backstop down.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.input_safety import disclosure_present
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _seed() -> tuple[uuid.UUID, str]:
    admin = uuid.uuid4()
    embed_key = f"vm_live_{uuid.uuid4().hex[:10]}"
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=admin,
                admin_id=admin,
                name="Sarah Chen",
                state=LucielState.active,
                embed_key_public_id=embed_key,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return admin, embed_key


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_first_reply_does_not_repeat_the_bootstrap_disclosure(client):
    admin, embed_key = await _seed()
    boot = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert boot.status_code == 200
    opening = boot.json()["openingMessage"]
    assert disclosure_present(opening)  # the bubble the visitor sees first

    r = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "what are your hours?"},
    )
    assert r.status_code == 200
    body = r.json()
    reply = body["reply"]["text"]
    assert not disclosure_present(reply), (
        "the disclosure was already delivered by the opening bubble — repeating it on "
        "the first reply is the double-greeting defect (C5)"
    )

    # The transcript's FIRST luciel row IS the greeting the visitor saw, so the
    # stored conversation matches the screen and the backstop has a prior turn.
    sid = uuid.UUID(body["sessionId"])
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(Transcript).where(Transcript.session_id == sid).order_by(Transcript.at)
                )
            )
            .scalars()
            .all()
        )
    assert rows[0].role == "luciel"
    assert rows[0].text == opening
    assert [r_.role for r_ in rows[1:]] == ["lead", "luciel"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_resumed_session_gets_no_second_greeting_row(client):
    _admin, embed_key = await _seed()
    first = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "hi there"},
    )
    sid = first.json()["sessionId"]
    second = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "and your prices?", "sessionId": sid},
    )
    assert second.status_code == 200
    assert not disclosure_present(second.json()["reply"]["text"])
