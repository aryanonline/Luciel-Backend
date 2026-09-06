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
async def test_bootstrap_never_doubles_assistant_in_name(client):
    """LIVE-VERIFIED (Harmony wave 2, item 1): a business/Luciel name that already
    ends in the word "Assistant" (e.g. "Aryan's Front-Desk Assistant"), with NO
    separate assistantName override configured, must not produce
    "...Assistant's assistant" in the opening message — the exact string the old
    default fallback (business + "'s assistant") plus the old greeting template
    (", the AI assistant for {business}") combined to produce on dev.
    """
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
                name="Aryan's Front-Desk Assistant",
                state=LucielState.active,
                embed_key_public_id=embed_key,
                channels=[],
                tools=[],
                escalation={},
                personality={},  # no assistantName override — exercises the fallback
            )
        )
        await s.flush()
    r = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert r.status_code == 200
    body = r.json()
    opening = body["openingMessage"]
    lowered = opening.lower()
    assert "assistant's assistant" not in lowered
    assert "assistant assistant" not in lowered
    # The Luciel name is used verbatim as the speaker.
    assert opening.startswith("Hi — I'm Aryan's Front-Desk Assistant.")
    # AI-disclosure requirement stays explicit.
    assert "ai assistant" in lowered


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
    # Since C5 (audit round 3) the disclosure is delivered ONCE, by the bootstrap
    # opening bubble (persisted as the session's first luciel row) — the first reply
    # must NOT repeat it (tests/contract/test_widget_single_disclosure.py pins the
    # full arc). The reply is the (echoed) answer content.
    assert "ai assistant" not in body["reply"]["text"].lower()
    assert "Echo:" in body["reply"]["text"]


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

    # A person's reply from the dashboard keeps its own role on the wire (2026-09-06
    # E2E walk, E2E-11): the widget labelled Sam's words with the AI's name.
    from datetime import UTC, datetime

    from app.models.conversation import Transcript

    async with tenant_session(_admin) as s:
        s.add(
            Transcript(
                admin_id=_admin,
                instance_id=_admin,
                session_id=uuid.UUID(sid),
                role="human_agent",
                text="Hi, this is Sam from the front desk.",
                at=datetime.now(UTC),
                deterministic=True,
            )
        )
        await s.flush()
    r2 = await client.get(f"/api/v1/chat-widget/sessions/{sid}?embedKey={embed_key}")
    human = [m for m in r2.json() if m["role"] == "human"]
    assert len(human) == 1
    assert human[0]["text"].startswith("Hi, this is Sam")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_reply_id_is_the_persisted_row_id_the_history_serves(client):
    """2026-09-06 E2E walk (E2E-4): the send response carried a random id while the
    history served the row id, so the widget's poll appended every reply a second
    time after a page restore. One reply, one id, on both wires."""
    _admin, embed_key = await _seed_active_instance()
    send = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "hi there"},
    )
    assert send.status_code == 200
    sid = send.json()["sessionId"]
    reply_id = send.json()["reply"]["messageId"]
    history = await client.get(f"/api/v1/chat-widget/sessions/{sid}?embedKey={embed_key}")
    assistant_ids = [m["messageId"] for m in history.json() if m["role"] == "assistant"]
    assert reply_id in assistant_ids
    assert assistant_ids[-1] == reply_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_beacon_ends_the_session_and_a_followup_opens_a_new_one(client):
    """§3.4.8 trigger (b): the widget close beacon ends THIS session (summarized,
    §3.4.7); a later message — even inside the 30-minute window — starts a NEW
    conversation. Repeat beacons are 204 no-ops."""
    from app.models.conversation import Session as SessionModel
    from app.models.conversation import SessionSummary
    from sqlalchemy import func, select

    admin, embed_key = await _seed_active_instance()

    first = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "Do you have openings?"},
    )
    sid = first.json()["sessionId"]

    end = await client.post(f"/api/v1/chat-widget/sessions/{sid}/end?embedKey={embed_key}")
    assert end.status_code == 204
    # Idempotent: the beacon can fire on both pagehide and visibilitychange.
    assert (
        await client.post(f"/api/v1/chat-widget/sessions/{sid}/end?embedKey={embed_key}")
    ).status_code == 204

    async with tenant_session(admin) as s:
        sess = (
            await s.execute(
                select(SessionModel).where(
                    SessionModel.admin_id == admin, SessionModel.id == uuid.UUID(sid)
                )
            )
        ).scalar_one()
        assert sess.ended_at is not None
        summaries = (
            await s.execute(
                select(func.count())
                .select_from(SessionSummary)
                .where(SessionSummary.admin_id == admin)
            )
        ).scalar_one()
        assert summaries == 1

    # A follow-up that still carries the old sessionId opens a NEW session: ended
    # means ended (§3.4.8) — a new budget unit, with the disclosure greeting again.
    followup = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "sessionId": sid, "text": "one more thing"},
    )
    assert followup.status_code == 200
    assert followup.json()["sessionId"] != sid

    async with tenant_session(admin) as s:
        count = (
            await s.execute(
                select(func.count()).select_from(SessionModel).where(SessionModel.admin_id == admin)
            )
        ).scalar_one()
        assert count == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_beacon_with_wrong_embed_key_is_rejected(client):
    """The beacon authenticates by embed key like every widget route — a garbage key
    never reaches the finalizer."""
    _admin, embed_key = await _seed_active_instance()
    send = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "hi"},
    )
    sid = send.json()["sessionId"]
    r = await client.post(f"/api/v1/chat-widget/sessions/{sid}/end?embedKey=vm_live_nope")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_widget_second_turn_resumes_the_same_session(client):
    """§9.5: a visitor's second message must continue the SAME conversation.

    Gating session lookup on resolved_lead_id meant a weak-identity widget visitor
    never matched anything, so every turn opened a fresh session — no memory, the
    disclosure re-sent on every reply, and each turn billed as its own conversation.
    """
    from app.models.conversation import Session as SessionModel
    from sqlalchemy import func, select

    admin, embed_key = await _seed_active_instance()

    first = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "Do you have openings?"},
    )
    sid = first.json()["sessionId"]

    second = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "sessionId": sid, "text": "What about Tuesday?"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["sessionId"] == sid, "second turn opened a new session"

    # The AI-identity disclosure is a first-contact obligation (§3.4.16); repeating it
    # every turn is the visible symptom of a session that never resumed.
    assert "ai assistant" not in second.json()["reply"]["text"].lower()

    # One session row, and the whole exchange lives in it.
    async with tenant_session(admin) as s:
        count = (
            await s.execute(
                select(func.count()).select_from(SessionModel).where(SessionModel.admin_id == admin)
            )
        ).scalar_one()
        assert count == 1, f"expected one session for the conversation, got {count}"

    history = await client.get(f"/api/v1/chat-widget/sessions/{sid}?embedKey={embed_key}")
    texts = [m["text"] for m in history.json()]
    assert any("Do you have openings?" in t for t in texts)
    assert any("What about Tuesday?" in t for t in texts)

    # §3.4.1b: a conversation is ONE budget unit however many turns it takes.
    async with tenant_session(admin) as s:
        state = await budget.check_budget(s, admin_id=admin, instance_id=admin)
    assert state.counted == 1, "a resumed session must not re-count against the budget"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payg_account_with_card_is_not_capped_past_the_free_allowance(client):
    """§12.9 / Vision §7: the free 50 is a cap only for accounts with no card.

    Every serving path used to pass has_payment_method=False, so a paying customer's
    widget went to at_cap at 50 — the exact opposite of what they are paying for.
    """
    from app.billing.metering import get_or_create_billing_account

    admin, embed_key = await _seed_active_instance()
    async with tenant_session(admin) as s:
        account = await get_or_create_billing_account(s, admin_id=admin)
        account.billing_state = "payg_enabled"
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
    assert body["renderState"] == "active", "a card on file must lift the free-50 cap"
    assert "Echo:" in body["reply"]["text"], "the tenant must still be served, not deflected"


async def test_widget_flood_is_rate_limited_before_budget(client):
    """B3 regression (Architecture §3.1.5): the widget /messages endpoint must run
    the pre-budget abuse gate BEFORE the budget gate / any LLM call, so a flood from
    one source can never drain a tenant's conversation budget (denial-of-wallet). A
    burst beyond the widget token bucket (burst=5, sustained 1/3s) returns HTTP 429
    and audits widget_abuse_blocked."""
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    admin, embed_key = await _seed_active_instance()
    sid = str(uuid.uuid4())  # one widget session token → one source bucket
    statuses = []
    for _ in range(8):  # exceed burst of 5 within the same instant (no refill)
        r = await client.post(
            "/api/v1/chat-widget/messages",
            json={"embedKey": embed_key, "sessionId": sid, "text": "hi"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, f"expected a 429 once the burst is exhausted, got {statuses}"
    # The 429 carries the documented error-contract body shape.
    blocked = [s for s in statuses if s == 429]
    assert blocked, "rate-limit must trigger"
    # And the block is audited as widget_abuse_blocked (no raw PII).
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(AdminAuditLog.event_type == "widget_abuse_blocked")
                )
            )
            .scalars()
            .all()
        )
    assert rows, "widget_abuse_blocked audit event must be written on a flood"
    for row in rows:
        assert "reason" in row.event_payload and row.event_payload["channel"] == "widget"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_widget_flood_with_rotating_session_ids_is_still_rate_limited(client):
    """Audit F046 (denial-of-wallet): the old bucket was keyed on the CALLER-supplied
    sessionId, so a fresh random id per request minted a fresh full bucket AND opened
    a new billed session each time. Now a made-up id buys nothing: the per-IP and
    new-session caps hold, and the tenant's conversation counter barely moves."""
    from app.models.conversation import Session
    from sqlalchemy import func, select

    admin, embed_key = await _seed_active_instance()
    statuses = []
    for _ in range(20):
        r = await client.post(
            "/api/v1/chat-widget/messages",
            json={"embedKey": embed_key, "sessionId": str(uuid.uuid4()), "text": "hi"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses, f"rotating session ids must not escape the limiter: {statuses}"
    opened = statuses.count(200)
    cap = __import__("app.channels.rate_limit", fromlist=["LIMITS"]).LIMITS["widget_session_open"]
    assert opened <= cap.burst, f"{opened} sessions opened from one address in one burst"
    async with tenant_session(admin) as s:
        n = (await s.execute(select(func.count()).select_from(Session))).scalar_one()
    assert n <= cap.burst, "a fabricated sessionId must not open (and bill) a fresh session"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_resumed_session_is_not_charged_against_the_new_session_cap(client):
    """The new-session cap must never throttle a real visitor continuing THEIR chat."""
    _admin, embed_key = await _seed_active_instance()
    first = await client.post(
        "/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": "hello"}
    )
    assert first.status_code == 200
    sid = first.json()["sessionId"]
    # Well past the new-session burst, all on the SAME open session: the per-session
    # bucket (burst 5) is what governs, and only the burst-exhaustion 429 may appear.
    statuses = []
    for _ in range(4):
        r = await client.post(
            "/api/v1/chat-widget/messages",
            json={"embedKey": embed_key, "sessionId": sid, "text": "more"},
        )
        statuses.append(r.status_code)
    assert statuses == [200, 200, 200, 200], statuses


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_history_is_scoped_to_widget_sessions_of_this_instance(client):
    """Audit F075: an embed key plus a guessed session id could read ANY channel's
    transcript of the tenant. The widget can only replay widget conversations."""
    from datetime import UTC, datetime

    from app.models.conversation import Session, Transcript

    admin, embed_key = await _seed_active_instance()
    sms_session = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Session(
                id=sms_session,
                admin_id=admin,
                instance_id=admin,
                channel="sms",
                mode="ai",
                counted=False,
                started_at=datetime.now(UTC),
                last_message_at=datetime.now(UTC),
            )
        )
        s.add(
            Transcript(
                admin_id=admin,
                instance_id=admin,
                session_id=sms_session,
                role="lead",
                text="my phone number is private",
            )
        )
        await s.flush()
    r = await client.get(
        f"/api/v1/chat-widget/sessions/{sms_session}", params={"embedKey": embed_key}
    )
    assert r.status_code == 200
    assert r.json() == [], "an SMS transcript must be invisible to the public widget key"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oversized_visitor_text_is_rejected_before_any_tenant_work(client):
    from app.schemas_api.widget import WIDGET_TEXT_MAX_CHARS

    _admin, embed_key = await _seed_active_instance()
    r = await client.post(
        "/api/v1/chat-widget/messages",
        json={"embedKey": embed_key, "text": "x" * (WIDGET_TEXT_MAX_CHARS + 1)},
    )
    assert r.status_code == 422
    r = await client.post("/api/v1/chat-widget/messages", json={"embedKey": embed_key, "text": ""})
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_bootstrap_storm_from_one_address_is_bounded(client):
    """The unauthenticated bootstrap performs tenant DB work; it gets its own ceiling."""
    _admin, embed_key = await _seed_active_instance()
    statuses = [
        (
            await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
        ).status_code
        for _ in range(14)
    ]
    assert 429 in statuses, statuses


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_bootstrap_disclosure_clause_never_names_the_luciel(client):
    """LIVE-VERIFIED regression: the opening greeting read "Hi — I'm Aryan's
    Front-Desk Assistant. I'm an AI assistant for Aryan's Front-Desk Assistant,
    and I can help…" — the disclosure clause substituted the LUCIEL name where
    the BUSINESS should be. No usable short business-name field exists on the
    Instance/Personality pillar, so the disclosure clause must fall back to
    "this business" and must never repeat the Luciel name.
    """
    luciel_name = "Aryan's Front-Desk Assistant"
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
                name=luciel_name,
                state=LucielState.active,
                embed_key_public_id=embed_key,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    r = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert r.status_code == 200
    body = r.json()
    opening = body["openingMessage"]
    assert opening.startswith(f"Hi — I'm {luciel_name}.")
    # The disclosure clause (after the speaker introduction) must name the
    # business generically -- never the Luciel name.
    clause = opening.split(".", 1)[1]
    assert luciel_name not in clause
    assert "an ai assistant for this business" in clause.lower()
    # The frozen wire field business_name still mirrors inst.name (unrelated to
    # the disclosure clause fix -- widget header contract, not the greeting text).
    assert body["businessName"] == luciel_name
