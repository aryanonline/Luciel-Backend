"""Contract conformance for the widget DATA PLANE /api/v1/chat-widget/*.

Mirrors widget-adapters.ts + schemas/widget.ts + plane-separation.test.ts:
  - bootstrap delivers the AI-identity disclosure + powered-by chrome (§3.4.16/§3.4.17)
  - at_cap render state returns a graceful, no-LLM reply (§3.4.1b)
  - send runs the loop and returns a camelCase WidgetSendResult
  - the widget plane has NO admin surface (no cookie auth; embed key authenticates)
  - an unknown embed key is rejected (404), never routed on a guess (§3.1.3)
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime import budget

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _seed_active_instance(*, personality: dict | None = None) -> tuple[uuid.UUID, str]:
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
                id=admin,  # any uuid; instance id is separate but resolution returns it
                admin_id=admin,
                name="Sarah Chen",
                state=LucielState.active,
                embed_key_public_id=embed_key,
                channels=[],
                tools=[],
                escalation={},
                personality=personality or {},
            )
        )
        await s.flush()
    return admin, embed_key


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_bootstrap_delivers_disclosure_and_chrome(client):
    _admin, embed_key = await _seed_active_instance(
        personality={"assistantName": "Sarah's assistant"}
    )
    r = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert r.status_code == 200
    body = r.json()
    assert body["renderState"] == "active"
    assert body["businessName"] == "Sarah Chen"
    assert body["assistantName"] == "Sarah's assistant"
    # AI-identity disclosure present + non-removable literals (§3.4.16/§3.4.17).
    assert "ai assistant" in body["openingMessage"].lower()
    assert body["aiAssistantLabel"] == "AI assistant"
    assert body["poweredByVantageMind"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unknown_embed_key_is_404(client):
    r = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": "vm_live_nope"})
    assert r.status_code == 404  # never routed on a guess (§3.1.3)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_runs_loop_and_returns_camelcase(client):
    _admin, embed_key = await _seed_active_instance()
    r = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "Do you have openings?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["renderState"] == "active"
    assert "sessionId" in body
    assert body["reply"]["role"] == "assistant"
    assert body["reply"]["text"].startswith("Echo:")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_at_cap_returns_graceful_reply(client):
    admin, embed_key = await _seed_active_instance()
    # Drive the tenant to cap (instance id == admin id here per the seed).
    async with tenant_session(admin) as s:
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=admin)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=admin, session_id=uuid.uuid4()
            )
    r = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "hello"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["renderState"] == "at_cap"
    # Graceful, no-LLM-style reply (§3.4.1b) — not an Echo of the input.
    assert "passed your message along" in body["reply"]["text"].lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_history_maps_roles_to_visitor_assistant(client):
    _admin, embed_key = await _seed_active_instance()
    send = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "hi there"},
    )
    sid = send.json()["sessionId"]
    r = await client.get(f"/api/v1/chat-widget/sessions/{sid}?embedKey={embed_key}")
    assert r.status_code == 200
    roles = {m["role"] for m in r.json()}
    # The widget never sees lead/luciel — only visitor/assistant.
    assert roles <= {"visitor", "assistant"}
