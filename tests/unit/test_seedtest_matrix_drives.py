"""Every SeedTest matrix combination, driven end to end against a stand-in API.

The judges are table-tested in test_seedtest_matrix.py; this runs each combination's
``drive`` → ``judge`` path for real (``run_combination``) against ``FakeLuciel`` — an
in-memory model of the documented contract served through ``httpx.MockTransport`` — so a
wiring mistake in a drive (a wrong path, a wrong wire key, an observation its judge does
not read) fails here instead of in the live run. The in-process parts (the counted-
conversation seeding, the escalation/audit reads) are stood in for too; nothing touches a
network or a database. Two misbehaving stand-ins prove the drives would catch the
regressions they exist for.
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from typing import Any

import httpx
import pytest

from tests.unit import seedtest_harness

H = seedtest_harness.load()

GROUNDED = "You have 30 days from the date of delivery to return an item."
ACK = "Of course - I've flagged this for the team, and they'll follow up with you shortly."
CLOSED = {"message": "Account closed.", "code": "account_closed", "detail": {}}
_ids = itertools.count(1)


def _id() -> str:
    return str(uuid.UUID(int=next(_ids)))


class FakeLuciel:
    """One tenant's API as documented, in memory. ``cut_counted`` / ``leak_closed`` turn on
    the two regressions the matrix must catch."""

    def __init__(self, *, cut_counted: bool = False, leak_closed: bool = False) -> None:
        from app.runtime.orchestrator import AT_CAP_REPLY

        self.at_cap_reply = AT_CAP_REPLY
        self.cut_counted, self.leak_closed = cut_counted, leak_closed
        self.state, self.closed, self.session_valid = "active", False, True
        self.grace_started: str | None = None
        self.embed_key = "vm_live_fake"
        self.counted, self.cap = 0, 50
        self.sessions: dict[str, dict[str, Any]] = {}
        self.channels = [{"id": "widget", "enabled": True}, {"id": "email", "enabled": False}]
        self.contact: str | None = None
        self.escalations: list[dict[str, Any]] = []
        self.leads: list[dict[str, Any]] = []

    # --- helpers ---------------------------------------------------------------- #
    def _session(self, sid: str | None) -> dict[str, Any] | None:
        return self.sessions.get(sid or "")

    def _luciel(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "embedKeyPublicId": self.embed_key,
            "channels": self.channels,
            "graceWindowStartedAt": self.grace_started,
        }

    def _answer(self, sid: str, text: str) -> str:
        if "$5,000" in text:
            self.escalations.append(
                {
                    "sessionId": sid,
                    "signal": "high_value_lead",
                    "reasons": [
                        "named a budget",
                        "wants it soon",
                        "ready to buy or book",
                        "answer only 0% backed by your knowledge",
                    ],
                }
            )
            return ACK
        own = re.search(r"My email is (\S+?)\.\s", text)
        if own:
            address = own.group(1).lower()
            self.leads.append(
                {
                    "leadId": _id(),
                    "contactIdentifier": address,
                    "email": address,
                    "phone": None,
                    "crmStatus": "never_attempted",
                    "crmDetail": "crm_not_connected",
                }
            )
        if "return" in text.lower() or "how many days" in text.lower():
            return GROUNDED
        return "Happy to help with that."

    # --- the routes ----------------------------------------------------------------- #
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content or b"{}") if request.content else {}
        if path == "/api/v1/auth/login":
            self.session_valid = True
            return httpx.Response(
                200, json={}, headers={"set-cookie": "luciel_session=tok; Path=/"}
            )
        if path.startswith("/api/v1/chat-widget/"):
            return self._widget(method, path, body, request)
        if path.startswith("/api/v1/admin/account"):
            return self._account(method, path)
        if not self.session_valid:
            return httpx.Response(401, json={"message": "Session revoked."})
        if self.closed and not self.leak_closed:
            return httpx.Response(403, json=CLOSED)
        return self._admin(method, path, body)

    def _widget(
        self, method: str, path: str, body: dict[str, Any], request: httpx.Request
    ) -> httpx.Response:
        if path.endswith("/bootstrap"):
            widget_on = any(c["id"] == "widget" and c["enabled"] for c in self.channels)
            sid = body.get("sessionId")
            boot_sess = self._session(sid) if sid else None
            in_flight = boot_sess is not None and not boot_sess["ended"]
            if self.state != "active":
                render, opening = "paused", ""
            elif not widget_on and not in_flight:
                # Owner decision D9 (this build's contract): a switched-off Website chat
                # channel renders nothing for a new visitor; an in-flight session finishes.
                render, opening = "channel_off", ""
            else:
                render = "at_cap" if self.counted >= self.cap else "active"
                opening = "I'm an AI assistant for this business."
            return httpx.Response(200, json={"renderState": render, "openingMessage": opening})
        if path.endswith("/messages"):
            sid, text = body.get("sessionId"), body["text"]
            sess = self._session(sid)
            in_flight = sess is not None and not sess["ended"]
            widget_on = any(c["id"] == "widget" and c["enabled"] for c in self.channels)
            if (self.state != "active" or not widget_on) and not in_flight:
                return httpx.Response(
                    200,
                    json={
                        "sessionId": sid or _id(),
                        "reply": {"text": ""},
                        "renderState": "paused" if self.state != "active" else "channel_off",
                    },
                )
            if not in_flight:
                sid = _id()
                sess = {"counted": False, "mode": "ai", "ended": False, "messages": []}
                self.sessions[sid] = sess
            assert sess is not None
            sess["messages"].append({"messageId": _id(), "role": "lead", "text": text})
            at_cap = self.counted >= self.cap and (not sess["counted"] or self.cut_counted)
            if at_cap:
                reply, render = self.at_cap_reply, "at_cap"
            elif sess["mode"] == "human_controlled":
                reply, render = "", "active"
            else:
                if not sess["counted"]:
                    sess["counted"] = True
                    self.counted += 1
                reply, render = self._answer(str(sid), text), "active"
            sess["messages"].append({"messageId": _id(), "role": "luciel", "text": reply})
            return httpx.Response(
                200, json={"sessionId": sid, "reply": {"text": reply}, "renderState": render}
            )
        sid = path.split("/")[-1] if not path.endswith("/end") else path.split("/")[-2]
        sess = self._session(sid)
        if path.endswith("/end"):
            if sess is not None:
                sess["ended"] = True
            return httpx.Response(204)
        roles = {"lead": "visitor", "luciel": "assistant", "human_agent": "human"}
        return httpx.Response(
            200,
            json=[
                {"messageId": m["messageId"], "role": roles[m["role"]], "text": m["text"]}
                for m in (sess or {"messages": []})["messages"]
            ],
        )

    def _account(self, method: str, path: str) -> httpx.Response:
        if not self.session_valid:
            return httpx.Response(401, json={"message": "Session revoked."})
        if path.endswith("/close"):
            if self.closed:
                return httpx.Response(403, json=CLOSED)
            self.closed, self.session_valid = True, False
            self.state = "luciel_grace_window"
            return httpx.Response(204)
        if path.endswith("/export"):
            # The served shape: ExportResult has no alias generator (download_url).
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "download_url": "https://api.example.test/x",
                    "ttl_days": 7,
                    "emailOutcome": "recorded",
                },
            )
        if path.endswith("/reopen"):
            self.closed, self.state = False, "active"
            return httpx.Response(200, json={"state": "verified", "lucielRestored": True})
        state = "closed" if self.closed else "verified"
        return httpx.Response(200, json={"state": state, "canReopen": self.closed})

    def _admin(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        if path == "/api/v1/admin/luciel" and method == "GET":
            return httpx.Response(200, json=self._luciel())
        if path == "/api/v1/admin/luciel/channels":
            self.channels = body["channels"]
            return httpx.Response(200, json=self._luciel())
        if path == "/api/v1/admin/luciel/escalation":
            self.contact = body.get("primaryEmail")
            return httpx.Response(200, json=self._luciel())
        transitions = {
            "pause": ("active", "paused"),
            "resume": ("paused", "active"),
            "delete": ("active", "luciel_grace_window"),
            "restore": ("luciel_grace_window", "active"),
        }
        verb = path.rsplit("/", 1)[-1]
        if path.startswith("/api/v1/admin/luciel/") and verb in transitions:
            source, target = transitions[verb]
            if self.state != source:
                return httpx.Response(409, json={"message": "Illegal lifecycle transition"})
            self.state = target
            self.grace_started = "2026-10-19T05:00:00+00:00" if verb == "delete" else None
            return httpx.Response(200, json=self._luciel())
        if path == "/api/v1/admin/luciel/status":
            today = sum(1 for s in self.sessions.values() if s["counted"])
            return httpx.Response(
                200, json={"today": {"conversations": today, "byChannel": {"widget": today}}}
            )
        if path == "/api/v1/admin/usage/overview":
            return httpx.Response(200, json={"conversationsThisPeriod": self.counted})
        if path == "/api/v1/dashboard/escalations":
            return httpx.Response(200, json=self.escalations)
        if path == "/api/v1/dashboard/leads":
            return httpx.Response(200, json=self.leads)
        match = re.fullmatch(r"/api/v1/dashboard/conversations/([^/]+)/(take-over|messages)", path)
        if match:
            sess = self._session(match.group(1))
            if sess is None:
                return httpx.Response(404, json={"message": "Not found."})
            if match.group(2) == "take-over":
                if sess["ended"]:
                    return httpx.Response(
                        409, json={"message": "This conversation has ended (session_ended)."}
                    )
                sess["mode"] = "human_controlled"
                return httpx.Response(200, json={"sessionId": match.group(1), "mode": sess["mode"]})
            if method == "POST":
                row = {"messageId": _id(), "role": "human_agent", "text": body["text"]}
                sess["messages"].append(row)
                return httpx.Response(
                    200,
                    json={"message": row, "delivered": False, "deliveryDetail": "widget_poll_only"},
                )
            return httpx.Response(200, json=sess["messages"])
        return httpx.Response(200, json={})


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Tenants backed by FakeLuciel; the in-process reads stood in for."""
    world: dict[str, Any] = {"fakes": {}, "options": {}}

    def seed(self: Any) -> None:
        fake = FakeLuciel(**world["options"])
        world["fakes"][self.role] = fake
        self.client.close()
        self.client = httpx.Client(transport=httpx.MockTransport(fake))
        self.embed_key = fake.embed_key

    async def admin_id_for(_email: str) -> uuid.UUID:
        return uuid.uuid4()

    async def teardown(_tenants: list[Any]) -> None:
        return None

    async def seed_counted(email: str, n: int) -> int:
        fake = next(
            f
            for role, f in world["fakes"].items()
            if email.endswith(f"-{role}@seed.vantagemind.ai")
        )
        fake.counted += n
        return fake.counted

    async def delivery(_admin_id: uuid.UUID, session_id: str) -> list[dict[str, Any]]:
        fake = world["fakes"]["m3"]
        return [
            {"signal": e["signal"], "status": "delivered", "detail": "email:primary"}
            for e in fake.escalations
            if e["sessionId"] == session_id
        ]

    async def audit(_admin_id: uuid.UUID) -> list[dict[str, Any]]:
        fake = world["fakes"]["m3"]
        return [
            {
                "event": "escalation_notification_sent",
                "recipient": fake.contact,
                "role": "primary",
                "failure": None,
            }
        ]

    monkeypatch.setattr(H.Tenant, "seed", seed)
    monkeypatch.setattr(H, "_admin_id_for", admin_id_for)
    monkeypatch.setattr(H, "_teardown", teardown)
    monkeypatch.setattr(H, "seed_counted_conversations", seed_counted)
    monkeypatch.setattr(H, "_escalation_delivery", delivery)
    monkeypatch.setattr(H, "_notification_audit", audit)
    return world


@pytest.mark.parametrize("key", H.MATRIX_KEYS)
def test_each_combination_passes_against_the_documented_contract(
    stand_in: dict[str, Any], key: str
) -> None:
    combo = next(c for c in H.MATRIX if c.key == key)
    r = H.Results()
    H.run_combination(r, combo)
    (row,) = r.rows
    assert row.status == "pass", row.evidence
    assert row.kind == "matrix" and row.seconds is not None


def test_m7_proves_zero_calls_on_the_redis_counter_when_it_has_one(
    stand_in: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(H, "await_quiet_llm_window", lambda _admin: 0)
    monkeypatch.setattr(H, "llm_calls_in_window", lambda _admin: 0)
    r = H.Results()
    H.run_combination(r, next(c for c in H.MATRIX if c.key == "M7"))
    (row,) = r.rows
    assert row.status == "pass" and "proof=llmcap window 0->0" in row.evidence


def test_m7_catches_a_counted_conversation_cut_at_the_cap(stand_in: dict[str, Any]) -> None:
    stand_in["options"] = {"cut_counted": True}
    r = H.Results()
    H.run_combination(r, next(c for c in H.MATRIX if c.key == "M7"))
    (row,) = r.rows
    assert row.status == "fail" and "counted_conversation_not_cut=FAIL[" in row.evidence


def test_m8_catches_a_route_that_answers_a_closed_account(stand_in: dict[str, Any]) -> None:
    stand_in["options"] = {"leak_closed": True}
    r = H.Results()
    H.run_combination(r, next(c for c in H.MATRIX if c.key == "M8"))
    (row,) = r.rows
    assert row.status == "fail" and "everything_else_403_account_closed=FAIL[" in row.evidence


def test_main_runs_the_selected_combinations_and_ends_with_the_summary(
    stand_in: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(H, "_harness_process_posture", lambda: None)
    assert H.main(["--matrix", "--only", "M6,M4", "--strict"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("[seedtest] API_BASE=") and "matrix=M6,M4" in out[0]
    assert any(line.startswith("[PASS] M4 takeover (") for line in out)
    assert any(line.startswith("[PASS] M6 lifecycle:pause-resume-delete-restore (") for line in out)
    assert any(line.startswith("[matrix] NOT in the matrix") for line in out)
    assert out[-1].startswith(
        "[seedtest] SUMMARY result=PASS cases=2 passed=2 failed=0 skipped=0 matrix=2 "
        "matrix_passed=2 matrix_failed=0 strict=on unexpected_skips=0 seconds="
    )


def test_list_matrix_prints_the_catalogue_and_runs_nothing(
    stand_in: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert H.main(["--list-matrix"]) == 0
    out = capsys.readouterr().out
    assert all(f"{key} " in out for key in H.MATRIX_KEYS)
    assert stand_in["fakes"] == {}
