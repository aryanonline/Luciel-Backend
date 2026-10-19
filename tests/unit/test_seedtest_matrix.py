"""SeedTest ``--matrix`` (round 7 WP-12): the harness's own logic, without the live run.

Each combination's ``judge`` is pure: given the observations its ``drive`` collected over
the wire, it decides PASS/FAIL and names every check. The tables below hold a passing
observation per combination and, for each check, a mutation that must FAIL exactly that
check — the failure the combination exists to catch (a fabricated push, a cut-off
conversation, a leaked route on a closed account...). The pins tie the harness's inputs to
the app's own rules: the hot-lead message really scores a hot lead and an ungrounded
answer, the lead message really separates an own address from a colleague's, the closed
routes really are gated the way the harness expects.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from tests.unit import seedtest_harness

H = seedtest_harness.load()

AT_CAP = "Thanks for reaching out - I've passed your message along."
ACK = (
    "Of course - I've flagged this for the team, and they'll follow up with you shortly. "
    "So the team can reach you, what's your name and the best email or phone number?"
)


def _with(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(base)
    out.update(changes)
    return out


def _assert_pass(judge: Any, obs: dict[str, Any]) -> str:
    ok, evidence = judge(obs)
    assert ok, evidence
    assert "=FAIL[" not in evidence
    return evidence


def _assert_fails(judge: Any, obs: dict[str, Any], label: str) -> None:
    ok, evidence = judge(obs)
    assert not ok
    assert f"{label}=FAIL[" in evidence, evidence


# --- M1 ------------------------------------------------------------------------- #
M1 = {
    "render": "active",
    "reply": "You have 30 days from the date of delivery to return an item.",
    "today": {"conversations": 1, "byChannel": {"widget": 1}},
    "cap": 50,
    "seeded": 49,
    "boot_render": "at_cap",
    "counted_before": 50,
    "over_render": "at_cap",
    "over_reply": AT_CAP + " So the team can reach you, what's your name?",
    "counted_after": 50,
    "at_cap_reply": AT_CAP,
}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"reply": "Echo: How many days do I have to return an item? 30"}, "grounded_answer"),
        ({"reply": "Returns are accepted for a while."}, "grounded_answer"),
        ({"render": "at_cap"}, "grounded_answer"),
        ({"today": {"conversations": 0, "byChannel": {}}}, "today_card_counts"),
        ({"today": {"conversations": 1, "byChannel": {"email": 1}}}, "today_card_counts"),
        ({"boot_render": "active"}, "at_cap"),
        ({"over_reply": "Sure! We have three tablets in stock."}, "at_cap"),
        ({"over_render": "active"}, "at_cap"),
        ({"counted_after": 51}, "at_cap"),
    ],
)
def test_m1_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m1, M1)
    _assert_fails(H.judge_m1, _with(M1, **changes), label)


# --- M2 ------------------------------------------------------------------------- #
M2_PRE_D9 = {
    "d9_served": False,
    "put_status": 200,
    "widget_enabled_after": False,
    "boot_new": "active",
    "inflight_render": "active",
    "inflight_reply": "Yes, gifts have the same 30-day window.",
    "inflight_same_session": True,
}
M2_D9 = _with(M2_PRE_D9, d9_served=True, boot_new="channel_off")


def test_m2_says_which_contract_it_asserted() -> None:
    assert "contract=[pre-D9" in _assert_pass(H.judge_m2, M2_PRE_D9)
    assert "contract=[D9" in _assert_pass(H.judge_m2, M2_D9)


@pytest.mark.parametrize(
    ("base", "changes", "label"),
    [
        (M2_PRE_D9, {"inflight_render": "paused"}, "in_flight_keeps_answering"),
        (M2_PRE_D9, {"inflight_reply": ""}, "in_flight_keeps_answering"),
        (M2_PRE_D9, {"inflight_same_session": False}, "in_flight_keeps_answering"),
        (M2_PRE_D9, {"put_status": 422}, "toggle_accepted"),
        (M2_PRE_D9, {"boot_new": "paused"}, "new_visitor_current_contract"),
        # A build that SERVES channel_off but still renders the widget: the D9 gate broke.
        (M2_D9, {"boot_new": "active"}, "new_visitor_channel_off"),
        (M2_D9, {"widget_enabled_after": True}, "toggle_saved"),
        (M2_D9, {"inflight_render": "channel_off"}, "in_flight_keeps_answering"),
    ],
)
def test_m2_judge(base: dict[str, Any], changes: dict[str, Any], label: str) -> None:
    _assert_fails(H.judge_m2, _with(base, **changes), label)


def test_channel_off_is_read_from_this_builds_widget_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Literal

    import app.schemas_api.widget as widget

    assert H.channel_off_served() is False  # the tip this harness was built on
    monkeypatch.setattr(
        widget, "WidgetRenderStateWire", Literal["active", "paused", "at_cap", "channel_off"]
    )
    assert H.channel_off_served() is True


# --- M3 ------------------------------------------------------------------------- #
CONTACT = "seed+1-m3-contact@seed.vantagemind.ai"
OWNER = "seed+1-m3@seed.vantagemind.ai"
SENT_TO_CONTACT = {
    "event": "escalation_notification_sent",
    "recipient": CONTACT,
    "role": "primary",
    "failure": None,
}
OWNER_COPY = {
    "event": "escalation_notification_sent",
    "recipient": OWNER,
    "role": "owner_unverified_copy",
    "failure": None,
}
OWNER_FALLBACK_SENT = {
    "event": "escalation_notification_sent",
    "recipient": OWNER,
    "role": "owner_fallback",
    "failure": None,
}
CONTACT_REFUSED = {
    "event": "escalation_delivery_failed",
    "recipient": CONTACT,
    "role": "primary",
    "failure": "ses_error:ClientError",
}
FALLBACK_AUDIT = {"event": "escalation_owner_fallback", "recipient": "", "role": None}
REASONS = [
    "named a budget",
    "wants it soon",
    "ready to buy or book",
    "answer only 0% backed by your knowledge",
]
M3 = {
    "render": "active",
    "reply": ACK,
    "rows": [{"sessionId": "s1", "signal": "high_value_lead", "reasons": REASONS}],
    "delivery": [{"signal": "high_value_lead", "status": "delivered", "detail": "email:primary"}],
    "audit": [SENT_TO_CONTACT, OWNER_COPY, FALLBACK_AUDIT],
    "contact": CONTACT,
    "owner": OWNER,
}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"reply": "Let me connect you with a person right now."}, "honest_handoff_ack"),
        ({"reply": ACK + " I'm connecting you with a live agent."}, "honest_handoff_ack"),
        ({"reply": "The Mavic 3 Pro battery holds 5000 mAh."}, "honest_handoff_ack"),
        ({"rows": []}, "one_escalation_row"),
        ({"rows": M3["rows"] * 2}, "one_escalation_row"),
        (
            {"rows": [{"sessionId": "s1", "signal": "cannot_answer", "reasons": REASONS}]},
            "both_reasons",
        ),
        (
            {"rows": [{"sessionId": "s1", "signal": "high_value_lead", "reasons": REASONS[:3]}]},
            "both_reasons",
        ),
        (
            {"rows": [{"sessionId": "s1", "signal": "high_value_lead", "reasons": REASONS[3:]}]},
            "both_reasons",
        ),
        (
            {"delivery": [{"signal": "high_value_lead", "status": "pending", "detail": None}]},
            "notification_honest",
        ),
        ({"audit": [OWNER_COPY]}, "notification_honest"),
    ],
)
def test_m3_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m3, M3)
    _assert_fails(H.judge_m3, _with(M3, **changes), label)


def test_m3_accepts_a_low_confidence_reason_as_the_answer_half() -> None:
    rows = [
        {
            "sessionId": "s1",
            "signal": "high_value_lead",
            "reasons": ["named a budget", "the model was 30% sure"],
        }
    ]
    _assert_pass(H.judge_m3, _with(M3, rows=rows))


@pytest.mark.parametrize(
    ("status", "detail", "audit", "ok"),
    [
        ("delivered", "email:primary", [SENT_TO_CONTACT, OWNER_COPY], True),
        ("sent", "email:primary", [SENT_TO_CONTACT], True),
        # "delivered" with no recorded send to the contact is a fabricated delivery.
        ("delivered", "email:primary", [OWNER_COPY], False),
        ("delivered", "sms:primary", [SENT_TO_CONTACT], False),
        ("owner_fallback", "refused", [CONTACT_REFUSED, OWNER_FALLBACK_SENT], True),
        # owner-only while the contact DID hear: the row understates who was reached.
        ("owner_fallback", "refused", [SENT_TO_CONTACT, OWNER_FALLBACK_SENT], False),
        ("owner_fallback", "refused", [CONTACT_REFUSED], False),
        ("queued", "ses_error:ClientError", [CONTACT_REFUSED], True),
        ("failed", "malformed_contact", [CONTACT_REFUSED], True),
        ("queued", None, [], False),
        ("queued", None, [SENT_TO_CONTACT], False),
        # Never settled, or an answer that is not a delivery truth for an email page.
        ("pending", None, [SENT_TO_CONTACT], False),
        ("unknown", None, [SENT_TO_CONTACT], False),
        ("unconfirmed", None, [SENT_TO_CONTACT], False),
    ],
)
def test_the_notification_row_must_agree_with_the_audit_trail(
    status: str, detail: str | None, audit: list[dict[str, Any]], ok: bool
) -> None:
    got, evidence = H.judge_notification(
        {"status": status, "detail": detail}, audit, contact=CONTACT, owner=OWNER
    )
    assert got is ok, evidence
    assert f"row={status}" in evidence


def test_no_escalation_row_is_not_an_honest_answer() -> None:
    assert H.judge_notification({}, [SENT_TO_CONTACT], contact=CONTACT, owner=OWNER)[0] is False


def test_the_hot_lead_question_scores_a_hot_lead_and_an_ungrounded_answer() -> None:
    """The M3 message must produce BOTH halves under the app's own gate 2."""
    from app.runtime.escalation import Signal, evaluate_outcome, high_value_lead_reasons

    score, reasons = high_value_lead_reasons(H.HOT_UNGROUNDED_QUESTION)
    assert score >= 0.5
    assert set(reasons) <= set(H.LEAD_REASONS) and reasons
    decision = evaluate_outcome(
        message=H.HOT_UNGROUNDED_QUESTION, plan_confidence=1.0, grounding_score=0.0
    )
    assert decision.signal == Signal.high_value_lead
    assert decision.answer_ungrounded
    obs = _with(
        M3,
        rows=[
            {"sessionId": "s1", "signal": decision.signal.value, "reasons": list(decision.reasons)}
        ],
    )
    _assert_pass(H.judge_m3, obs)


def test_the_ack_pattern_is_the_runtimes_own() -> None:
    from app.runtime import orchestrator

    assert H.HANDOFF_ACK_RE.pattern == orchestrator._HANDOFF_ACK_MARK.pattern
    assert H.HANDOFF_ACK_RE.search("I've passed this along to the team - they'll follow up.")
    for short in orchestrator.SHORT_HANDOFF_ACKS:
        assert not H.REALTIME_CLAIM_RE.search(short)


# --- M4 ------------------------------------------------------------------------- #
HUMAN = "Hi, this is the team picking this up (seedtest 1)."
M4 = {
    "take_status": 200,
    "take_mode": "human_controlled",
    "reply_status": 200,
    "delivered": False,
    "delivery_detail": "widget_poll_only",
    "message_id": "m1",
    "human_text": HUMAN,
    "stored": [
        {"messageId": "m0", "role": "lead", "text": "Hi"},
        {"messageId": "m1", "role": "human_agent", "text": HUMAN},
    ],
    "poll": [
        {"messageId": "m0", "role": "visitor", "text": "Hi"},
        {"messageId": "m1", "role": "human", "text": HUMAN},
    ],
    "end_status": 204,
    "refused_status": 409,
    "refused_message": "This conversation has ended, so there is nothing to take over or "
    "reply into (session_ended).",
}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"take_mode": "ai"}, "take_over"),
        ({"take_status": 404}, "take_over"),
        # The widget has no push transport: a "delivered" reply is a fabricated send.
        ({"delivered": True}, "human_reply_persisted_honestly"),
        ({"delivery_detail": None}, "human_reply_persisted_honestly"),
        ({"stored": M4["stored"][:1]}, "human_reply_persisted_honestly"),
        ({"message_id": ""}, "human_reply_persisted_honestly"),
        # Served under the AI's name: a person is never presented as the assistant.
        (
            {"poll": [{"messageId": "m1", "role": "assistant", "text": HUMAN}]},
            "served_by_widget_poll",
        ),
        ({"poll": M4["poll"][:1]}, "served_by_widget_poll"),
        ({"refused_status": 200}, "ended_session_refused"),
        ({"refused_message": "Conflict."}, "ended_session_refused"),
        ({"end_status": 404}, "ended_session_refused"),
    ],
)
def test_m4_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m4, M4)
    _assert_fails(H.judge_m4, _with(M4, **changes), label)


# --- M5 ------------------------------------------------------------------------- #
OWN = "seed+1-m5-own@seed.vantagemind.ai"
COLLEAGUE = "seed+1-m5-colleague@seed.vantagemind.ai"
LEAD = {
    "leadId": "l1",
    "contactIdentifier": OWN,
    "email": OWN,
    "phone": None,
    "crmStatus": "never_attempted",
    "crmDetail": "crm_not_connected",
}
M5 = {"render": "active", "own": OWN, "colleague": COLLEAGUE, "leads": [LEAD]}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"leads": []}, "own_email_is_the_identifier"),
        ({"leads": [_with(LEAD, contactIdentifier=COLLEAGUE)]}, "own_email_is_the_identifier"),
        ({"leads": [_with(LEAD, contactIdentifier=COLLEAGUE)]}, "third_party_is_not"),
        (
            {
                "leads": [
                    LEAD,
                    _with(LEAD, leadId="l2", contactIdentifier=COLLEAGUE, email=COLLEAGUE),
                ]
            },
            "third_party_is_not",
        ),
        ({"leads": [_with(LEAD, crmStatus="synced")]}, "crm_never_attempted"),
        ({"leads": [_with(LEAD, crmDetail="not_yet_pushed")]}, "crm_never_attempted"),
        ({"leads": []}, "crm_never_attempted"),
    ],
)
def test_m5_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m5, M5)
    _assert_fails(H.judge_m5, _with(M5, **changes), label)


def test_m5_compares_addresses_case_insensitively() -> None:
    _assert_pass(H.judge_m5, _with(M5, leads=[_with(LEAD, contactIdentifier=OWN.upper())]))


def test_the_lead_message_separates_the_own_address_from_the_colleagues() -> None:
    """The M5 wording, through the app's own extractor: own claimed, colleague mentioned."""
    from app.runtime.lead_capture import extract_lead_signals

    text = (
        f"Hi, I'm Jordan Lee. My email is {OWN}. Please also copy my colleague {COLLEAGUE} "
        "on the quote for a refurbished laptop."
    )
    signals = extract_lead_signals(text)
    assert signals.email == OWN
    assert COLLEAGUE in signals.mentions


# --- M6 ------------------------------------------------------------------------- #
M6 = {
    "key0": "vm_live_abc",
    "pause_state": "paused",
    "boot_paused": "paused",
    "boot_paused_opening": "",
    "send_paused": "paused",
    "send_paused_reply": "",
    "resume_state": "active",
    "boot_resumed": "active",
    "delete_state": "luciel_grace_window",
    "grace_started": "2026-10-19T05:00:00+00:00",
    "boot_deleted": "paused",
    "restore_state": "active",
    "key_after": "vm_live_abc",
    "boot_restored": "active",
}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"boot_paused": "active"}, "pause_renders_nothing"),
        (
            {"boot_paused_opening": "I'm an AI assistant for this business."},
            "pause_renders_nothing",
        ),
        ({"send_paused_reply": "Hi! How can I help?"}, "pause_renders_nothing"),
        ({"pause_state": "HTTP 409: Illegal lifecycle transition"}, "pause_renders_nothing"),
        ({"resume_state": "paused"}, "resume_active"),
        ({"boot_resumed": "paused"}, "resume_active"),
        ({"boot_deleted": "active"}, "delete_grace_renders_nothing"),
        ({"grace_started": None}, "delete_grace_renders_nothing"),
        ({"key_after": "vm_live_def"}, "restore_keeps_embed_key"),
        ({"boot_restored": "paused"}, "restore_keeps_embed_key"),
    ],
)
def test_m6_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m6, M6)
    _assert_fails(H.judge_m6, _with(M6, **changes), label)


# --- M7 ------------------------------------------------------------------------- #
M7 = {
    "cap": 50,
    "seeded": 49,
    "last_free_render": "active",
    "counted_at_cap": 50,
    "calls_before": 0,
    "calls_after": 0,
    "over_render": "at_cap",
    "over_reply": AT_CAP,
    "counted_after_over": 50,
    "mid_render": "active",
    "mid_reply": "You have 30 days from the date of delivery.",
    "mid_same_session": True,
    "counted_after_mid": 50,
    "at_cap_reply": AT_CAP,
}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"last_free_render": "at_cap"}, "fiftieth_served"),
        ({"counted_at_cap": 49}, "fiftieth_served"),
        ({"over_render": "active"}, "fifty_first_at_cap"),
        ({"over_reply": "Here is a generated answer."}, "fifty_first_at_cap"),
        ({"counted_after_over": 51}, "fifty_first_at_cap"),
        # A model call during the at-cap turn shows on the API's own counter.
        ({"calls_after": 1}, "zero_model_calls"),
        ({"calls_before": None, "calls_after": None, "counted_after_over": 51}, "zero_model_calls"),
        # The Phase 1 regression: the 50th conversation cut after its counting turn.
        ({"mid_render": "at_cap"}, "counted_conversation_not_cut"),
        ({"mid_reply": AT_CAP}, "counted_conversation_not_cut"),
        ({"mid_same_session": False}, "counted_conversation_not_cut"),
        ({"counted_after_mid": 51}, "counted_conversation_not_cut"),
    ],
)
def test_m7_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m7, M7)
    _assert_fails(H.judge_m7, _with(M7, **changes), label)


def test_m7_without_a_redis_probe_says_its_proof_is_the_counter() -> None:
    evidence = _assert_pass(H.judge_m7, _with(M7, calls_before=None, calls_after=None))
    assert "proof=counter-only" in evidence


def test_the_quiet_window_wait_returns_the_first_empty_reading() -> None:
    readings = iter([3, 1, 0, 5])
    slept: list[float] = []
    got = H.await_quiet_llm_window(
        uuid.uuid4(), read=lambda _a: next(readings), sleep=slept.append, clock=lambda: 0.0
    )
    assert got == 0 and slept == [2.0, 2.0]


def test_the_quiet_window_wait_is_bounded() -> None:
    clock = iter([0.0, 10.0, 80.0])
    got = H.await_quiet_llm_window(
        uuid.uuid4(),
        timeout=75.0,
        read=lambda _a: 2,
        sleep=lambda _s: None,
        clock=lambda: next(clock),
    )
    assert got == 2


def test_no_probe_means_no_wait() -> None:
    got = H.await_quiet_llm_window(
        uuid.uuid4(),
        read=lambda _a: None,
        sleep=lambda _s: pytest.fail("waited"),
        clock=lambda: 0.0,
    )
    assert got is None


def test_the_call_counter_reads_the_apis_redis_key_and_never_fabricates_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.runtime import llm_cap

    admin = uuid.uuid4()
    monkeypatch.setattr(llm_cap, "_store", llm_cap.InMemoryLLMCapStore())
    assert H.llm_calls_in_window(admin) is None  # in-memory: the API's calls are invisible

    class FakeRedis:
        def __init__(self, value: str | None, *, broken: bool = False) -> None:
            self.value, self.broken, self.keys = value, broken, []

        def get(self, key: str) -> str | None:
            self.keys.append(key)
            if self.broken:
                raise ConnectionError("redis down")
            return self.value

    healthy = FakeRedis("7")
    monkeypatch.setattr(llm_cap, "_store", llm_cap.RedisLLMCapStore(healthy))
    assert H.llm_calls_in_window(admin) == 7
    assert healthy.keys == [f"llmcap:{admin}"]
    monkeypatch.setattr(llm_cap, "_store", llm_cap.RedisLLMCapStore(FakeRedis(None)))
    assert H.llm_calls_in_window(admin) == 0  # no key: the window is empty
    monkeypatch.setattr(llm_cap, "_store", llm_cap.RedisLLMCapStore(FakeRedis("3", broken=True)))
    assert H.llm_calls_in_window(admin) is None


# --- M8 ------------------------------------------------------------------------- #
M8 = {
    "close_status": 204,
    "stale_status": 401,
    "account_status": 200,
    "account": {"state": "closed", "canReopen": True},
    "export_status": 200,
    "export_has_link": True,
    "refused": {f"{m} {p}": [403, "account_closed"] for m, p, _ in H.CLOSED_REFUSED_ROUTES},
    "reopen_status": 200,
    "reopen": {"state": "verified", "lucielRestored": True},
    "luciel_status": 200,
    "luciel_state": "active",
    "leads_status": 200,
    "boot": "active",
}


def _leak(route: str, got: list[object]) -> dict[str, Any]:
    refused = dict(M8["refused"])
    refused[route] = got
    return {"refused": refused}


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"close_status": 409}, "closed"),
        ({"stale_status": 200}, "old_session_revoked"),
        ({"account": {"state": "closed", "canReopen": False}}, "reduced_surface_answers"),
        ({"account_status": 403}, "reduced_surface_answers"),
        ({"export_status": 403}, "reduced_surface_answers"),
        ({"export_has_link": False}, "reduced_surface_answers"),
        (_leak("GET /api/v1/dashboard/leads", [200, None]), "everything_else_403_account_closed"),
        # A 403 the frontend cannot tell from "log in again" is not the D5 refusal.
        (_leak("GET /api/v1/billing", [403, None]), "everything_else_403_account_closed"),
        ({"refused": {}}, "everything_else_403_account_closed"),
        ({"reopen": {"state": "verified", "lucielRestored": False}}, "reopen"),
        ({"reopen_status": 409}, "reopen"),
        ({"luciel_state": "luciel_grace_window"}, "reopen_restores"),
        ({"boot": "paused"}, "reopen_restores"),
        ({"leads_status": 403}, "reopen_restores"),
    ],
)
def test_m8_judge(changes: dict[str, Any], label: str) -> None:
    _assert_pass(H.judge_m8, M8)
    _assert_fails(H.judge_m8, _with(M8, **changes), label)


def test_m8_reads_the_export_link_from_the_key_the_api_serves() -> None:
    """ExportResult has no alias generator, so the link goes out as ``download_url``
    (``emailOutcome`` alone is aliased). M8 read ``downloadUrl`` and failed against the
    real app; it now reads the served key (and the camelCase one, should the wire be
    fixed later)."""
    from app.api.v1.admin.account import ExportResult

    served = ExportResult(
        download_url="https://api.example.test/api/v1/exports/t",
        ttl_days=7,
        email_outcome="recorded",
    ).model_dump(mode="json", by_alias=True)
    assert H.export_link(served) == "https://api.example.test/api/v1/exports/t"
    assert H.export_link({"downloadUrl": "https://x.test/e"}) == "https://x.test/e"
    assert H.export_link({"ok": True, "download_url": ""}) == ""
    assert H.export_link({}) == ""


def test_the_closure_route_lists_are_real_routes() -> None:
    from tests.unit.test_route_registry import EXPECTED_ROUTES

    for method, path, _body in H.CLOSED_REFUSED_ROUTES:
        assert (method, path) in EXPECTED_ROUTES
    for method, path in H.CLOSED_REDUCED_ROUTES:
        assert (method, path) in EXPECTED_ROUTES


def test_the_closure_route_lists_match_the_apps_dependencies() -> None:
    """Refused routes hang on current_admin (refuses a closed account); the reduced D5
    surface on current_account_owner. The harness's expectation is the app's wiring."""
    from app.api.deps import current_account_owner, current_admin
    from app.main import create_app
    from fastapi.routing import APIRoute

    def api_routes(routes: list[Any]) -> list[APIRoute]:
        # Newer FastAPI nests included routers (route.original_router) instead of
        # flattening them into app.routes; walk both shapes.
        found: list[APIRoute] = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            nested = getattr(getattr(route, "original_router", None), "routes", None)
            if isinstance(nested, list):
                found.extend(api_routes(nested))
        return found

    mounted = api_routes(list(create_app().routes))

    def deps_of(method: str, path: str) -> set[object]:
        for route in mounted:
            if route.path == path and method in route.methods:
                return {d.call for d in route.dependant.dependencies}
        raise AssertionError(f"{method} {path} not mounted")

    for method, path, _body in H.CLOSED_REFUSED_ROUTES:
        assert current_admin in deps_of(method, path), path
    for method, path in H.CLOSED_REDUCED_ROUTES:
        found = deps_of(method, path)
        assert current_account_owner in found and current_admin not in found, path


# --- the runner ----------------------------------------------------------------- #
def test_the_matrix_is_the_eight_combinations_the_owner_asked_for() -> None:
    assert H.MATRIX_KEYS == ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8")
    assert len(H.MANUAL_WALK_ITEMS) == 5  # SMS/voice, mailbox, Meta, CRM, calendar


@pytest.fixture
def offline_tenants(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """No network: seeding, admin-id lookup and teardown are recorded, not performed."""
    calls: dict[str, list[str]] = {"seeded": [], "purged": []}

    def seed(self: Any) -> None:
        calls["seeded"].append(self.role)
        self.embed_key = "vm_live_test"

    async def admin_id_for(_email: str) -> uuid.UUID:
        return uuid.uuid4()

    async def teardown(tenants: list[Any]) -> None:
        calls["purged"].extend(t.role for t in tenants)

    monkeypatch.setattr(H.Tenant, "seed", seed)
    monkeypatch.setattr(H, "_admin_id_for", admin_id_for)
    monkeypatch.setattr(H, "_teardown", teardown)
    return calls


def test_a_combination_records_its_wall_clock_and_purges_its_tenant(
    offline_tenants: dict[str, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    combo = H.Combination("M9", "probe", "-", lambda t: {"ok": True}, lambda o: (True, "fine"))
    r = H.Results()
    H.run_combination(r, combo)
    (row,) = r.rows
    assert (row.name, row.status, row.kind) == ("M9 probe", "pass", "matrix")
    assert row.seconds is not None and row.seconds >= 0
    assert offline_tenants == {"seeded": ["m9"], "purged": ["m9"]}
    assert "[PASS] M9 probe (" in capsys.readouterr().out


def test_a_combination_that_cannot_run_is_a_fail_and_still_purges(
    offline_tenants: dict[str, list[str]],
) -> None:
    def drive(_t: Any) -> dict[str, Any]:
        raise RuntimeError("widget_send: HTTP 500")

    r = H.Results()
    H.run_combination(r, H.Combination("M9", "probe", "-", drive, H.judge_m1))
    (row,) = r.rows
    assert row.status == "fail" and row.evidence.startswith("aborted: RuntimeError")
    assert offline_tenants["purged"] == ["m9"]


def test_keep_leaves_the_tenant(offline_tenants: dict[str, list[str]]) -> None:
    combo = H.Combination("M9", "probe", "-", lambda t: {}, lambda o: (True, "fine"))
    H.run_combination(H.Results(), combo, keep=True)
    assert offline_tenants["purged"] == []


def test_only_selects_combinations(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(H, "run_combination", lambda _r, combo, keep=False: ran.append(combo.key))
    H.run_matrix(H.Results(), ("M3", "M8"))
    assert ran == ["M3", "M8"]
    ran.clear()
    H.run_matrix(H.Results(), ())
    assert ran == list(H.MATRIX_KEYS)


@pytest.mark.parametrize(
    ("argv", "cases", "matrix", "only"),
    [
        ([], True, False, ()),
        (["--matrix"], False, True, ()),
        (["--matrix", "--cases"], True, True, ()),
        (["--matrix", "--only", "m3, M8,M3"], False, True, ("M3", "M8")),
    ],
)
def test_cli_selection(argv: list[str], cases: bool, matrix: bool, only: tuple[str, ...]) -> None:
    opts = H.parse_args(argv, {})
    assert (opts.cases, opts.matrix, opts.only) == (cases, matrix, only)


@pytest.mark.parametrize("argv", [["--matrix", "--only", "M9"], ["--only", "M3"]])
def test_cli_refuses_a_bad_selection(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        H.parse_args(argv, {})
