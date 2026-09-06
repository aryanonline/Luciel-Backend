"""Paused/deleted widget honesty + the in-flight drain (§3.6.2/§3.6.3 — C7/C8).

Live defect: ``channel_resolve_embed_key`` admitted only ``active``, so a paused
Luciel's widget got HTTP 404 — the documented graceful paused render was dead
code, and a visitor mid-conversation at the pause instant saw a hard failure.
Now: a paused/grace instance resolves; NEW visitors get the quiet
``render_state="paused"`` directive; a session already in flight FINISHES on its
channel (the §3.6.2 carve-out and the §3.6.3 fused-quiesce drain).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import state_machine
from app.models.instance import Account, AccountState, Instance, LucielState

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
                name="L",
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
async def test_paused_widget_serves_the_paused_directive_never_404(client):
    admin, embed_key = await _seed()
    async with tenant_session(admin) as s:
        await state_machine.pause(s, admin, actor_user_id=admin)

    boot = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert boot.status_code == 200, "a paused Luciel must render quietly, never 404"
    assert boot.json()["renderState"] == "paused"
    assert boot.json()["openingMessage"] == ""

    r = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hello?"}
    )
    assert r.status_code == 200
    assert r.json()["renderState"] == "paused"
    assert r.json()["reply"]["text"] == ""


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_in_flight_widget_session_finishes_across_a_pause(client):
    admin, embed_key = await _seed()
    first = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hi there"}
    )
    sid = first.json()["sessionId"]
    async with tenant_session(admin) as s:
        await state_machine.pause(s, admin, actor_user_id=admin)

    # The in-flight session drains: the same conversation keeps answering (§3.6.2
    # carve-out — the lead is never dropped mid-sentence by the owner's pause)...
    cont = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "and your prices?", "sessionId": sid},
    )
    assert cont.status_code == 200
    assert cont.json()["renderState"] == "active"
    assert "Echo:" in cont.json()["reply"]["text"]

    # ...while a NEW visitor (no session) is held until resume.
    fresh = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hello?"}
    )
    assert fresh.json()["renderState"] == "paused"

    # The same visitor clicks to the NEXT PAGE of the site: the new page's widget
    # boots with the tab's stored session id and must still render (2026-09-06 E2E
    # walk, E2E-9). A visitor without one, or with a made-up id, is held.
    boot_mid = await client.post(
        "/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key, "sessionId": sid}
    )
    assert boot_mid.json()["renderState"] == "active"
    assert boot_mid.json()["openingMessage"]  # the real chrome, not the empty directive
    boot_new = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert boot_new.json()["renderState"] == "paused"
    boot_fake = await client.post(
        "/api/v1/chat-widget/bootstrap",
        json={"embedKey": embed_key, "sessionId": str(uuid.uuid4())},
    )
    assert boot_fake.json()["renderState"] == "paused"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_drains_in_flight_and_refuses_new_intake(client):
    admin, embed_key = await _seed()
    first = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hi there"}
    )
    sid = first.json()["sessionId"]
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)

    # §3.6.3 fused quiesce: the conversation in flight at the delete instant
    # finishes on its channel...
    cont = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "wait, one more question", "sessionId": sid},
    )
    assert cont.status_code == 200
    assert cont.json()["renderState"] == "active"

    # ...new intake is refused quietly (no broken UI on the customer's site)...
    fresh = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hello?"}
    )
    assert fresh.json()["renderState"] == "paused"
    boot = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert boot.status_code == 200
    assert boot.json()["renderState"] == "paused"

    # ...and restore brings the SAME key back fully active (§3.6.4).
    async with tenant_session(admin) as s:
        inst = await state_machine.restore(s, admin, actor_user_id=admin)
        assert inst.embed_key_public_id == embed_key
    after = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hello again"}
    )
    assert after.json()["renderState"] == "active"
