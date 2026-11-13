"""Owner decision D9 (round 7): the Website chat channel toggle is honoured.

Live defect: the channels pillar let the owner switch ``widget`` off, but the widget
data plane never read the flag — the bootstrap kept answering ``active`` with the AI
greeting, and the message route kept opening (and billing) new conversations for a
channel the owner had turned off.

Now, at the route level:
  - off + a NEW visitor → bootstrap answers ``channel_off`` (no greeting, no budget
    read, no LLM) and the message route opens NO session, answering ``channel_off``;
  - off + a session already in flight → that conversation finishes (the same carve-out
    the pause and the delete drain use);
  - switched back on → ``active`` on the very next bootstrap;
  - off while PAUSED → ``paused`` wins (documented in app/api/CLAUDE.md): both render
    nothing, but only ``channel_off`` keeps the widget re-checking.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.contract]

BOOT = "/api/v1/chat-widget/bootstrap"
SEND = "/api/v1/chat-widget/messages"


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"d9-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _luciel_with_widget(client) -> tuple[uuid.UUID, str]:
    admin_id = await _signup_verify_login(client)
    created = await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    assert created.status_code == 200
    return admin_id, created.json()["embedKeyPublicId"]


async def _set_widget(client, *, enabled: bool) -> None:
    """Flip Website chat exactly as the dashboard toggle does: full-replace PUT."""
    current = (await client.get("/api/v1/admin/luciel")).json()["channels"]
    channels = [
        {"id": c["id"], "enabled": enabled if c["id"] == "widget" else c["enabled"]}
        for c in current
    ]
    r = await client.put("/api/v1/admin/luciel/channels", json={"channels": channels})
    assert r.status_code == 200
    widget = next(c for c in r.json()["channels"] if c["id"] == "widget")
    assert widget["enabled"] is enabled


async def _session_count(admin_id: uuid.UUID) -> int:
    from app.db.session import tenant_session
    from app.models.conversation import Session as ConversationSession

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(ConversationSession)
                .where(ConversationSession.admin_id == admin_id)
            )
        ).scalar_one()


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A held answer must never reach the model (the same cost profile as paused)."""
    from app.api.v1 import chat_widget

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("a held widget answer must not build an LLM router")

    monkeypatch.setattr(chat_widget, "build_llm_router", _boom)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_channel_off_holds_a_new_visitor_without_a_session_or_llm(client, monkeypatch):
    admin_id, embed_key = await _luciel_with_widget(client)
    await _set_widget(client, enabled=False)
    _no_llm(monkeypatch)

    boot = await client.post(BOOT, json={"embedKey": embed_key})
    assert boot.status_code == 200
    body = boot.json()
    assert body["renderState"] == "channel_off"
    # Same shape as the paused directive: no greeting, honest platform literals.
    assert body["openingMessage"] == ""
    assert body["aiAssistantLabel"] == "AI assistant"
    assert body["poweredByVantageMind"] is True

    # A tab holding a made-up (or expired) session id is a new visitor too.
    fake = await client.post(BOOT, json={"embedKey": embed_key, "sessionId": str(uuid.uuid4())})
    assert fake.json()["renderState"] == "channel_off"

    sent = await client.post(SEND, json={"embedKey": embed_key, "text": "anyone there?"})
    assert sent.status_code == 200
    assert sent.json()["renderState"] == "channel_off"
    assert sent.json()["reply"]["text"] == ""
    # The message route refused to OPEN a conversation (opening one is the billable
    # event): nothing was persisted for the visitor.
    assert await _session_count(admin_id) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_channel_off_lets_the_conversation_in_flight_finish(client):
    admin_id, embed_key = await _luciel_with_widget(client)
    first = await client.post(SEND, json={"embedKey": embed_key, "text": "hi there"})
    assert first.json()["renderState"] == "active"
    sid = first.json()["sessionId"]

    await _set_widget(client, enabled=False)

    # The conversation already open keeps answering on its own session...
    cont = await client.post(
        SEND, json={"embedKey": embed_key, "text": "and your prices?", "sessionId": sid}
    )
    assert cont.status_code == 200
    assert cont.json()["renderState"] == "active"
    assert cont.json()["sessionId"] == sid
    assert "Echo:" in cont.json()["reply"]["text"]
    # ...and the same visitor's next page still renders the chat.
    boot_mid = await client.post(BOOT, json={"embedKey": embed_key, "sessionId": sid})
    assert boot_mid.json()["renderState"] == "active"
    assert boot_mid.json()["openingMessage"]

    # A different visitor is held, and no second conversation is opened.
    fresh = await client.post(SEND, json={"embedKey": embed_key, "text": "hello?"})
    assert fresh.json()["renderState"] == "channel_off"
    assert await _session_count(admin_id) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switching_the_channel_back_on_serves_active_on_the_next_bootstrap(client):
    admin_id, embed_key = await _luciel_with_widget(client)
    await _set_widget(client, enabled=False)
    held = await client.post(BOOT, json={"embedKey": embed_key})
    assert held.json()["renderState"] == "channel_off"

    await _set_widget(client, enabled=True)
    boot = await client.post(BOOT, json={"embedKey": embed_key})
    assert boot.json()["renderState"] == "active"
    assert boot.json()["openingMessage"]
    sent = await client.post(SEND, json={"embedKey": embed_key, "text": "hello again"})
    assert sent.json()["renderState"] == "active"
    assert await _session_count(admin_id) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_paused_wins_over_channel_off_and_in_flight_still_finishes(client, monkeypatch):
    from app.db.session import tenant_session
    from app.lifecycle import state_machine

    admin_id, embed_key = await _luciel_with_widget(client)
    first = await client.post(SEND, json={"embedKey": embed_key, "text": "hi there"})
    sid = first.json()["sessionId"]
    await _set_widget(client, enabled=False)
    async with tenant_session(admin_id) as s:
        await state_machine.pause(s, admin_id, actor_user_id=admin_id)

    # Paused AND off → "paused": the stronger, account-wide state; the widget stops
    # re-checking a Luciel that serves nobody (both render nothing).
    boot = await client.post(BOOT, json={"embedKey": embed_key})
    assert boot.json()["renderState"] == "paused"
    assert boot.json()["openingMessage"] == ""

    # The in-flight conversation still finishes under both holds.
    cont = await client.post(
        SEND, json={"embedKey": embed_key, "text": "one more thing", "sessionId": sid}
    )
    assert cont.json()["renderState"] == "active"

    _no_llm(monkeypatch)
    fresh = await client.post(SEND, json={"embedKey": embed_key, "text": "hello?"})
    assert fresh.json()["renderState"] == "paused"
    assert await _session_count(admin_id) == 1

    # Resumed but still off → channel_off (the toggle is still honoured).
    async with tenant_session(admin_id) as s:
        await state_machine.resume(s, admin_id, actor_user_id=admin_id)
    again = await client.post(BOOT, json={"embedKey": embed_key})
    assert again.json()["renderState"] == "channel_off"
