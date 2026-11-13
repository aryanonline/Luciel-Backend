"""Arc 15 — server-side enforcement of the LOCKED config invariants (5 pillars).

For EACH invariant: a test that the ILLEGAL mutation is rejected with the correct
status (422 validation_error / 409 conflict per transport.ts), AND a test that the
LEGAL mutation succeeds. Covers:

  * §6 / Arch §3.4.3 — unknown/extra fields (e.g. a model-selection field) rejected on
    EVERY pillar body (extra='forbid'); camelCase aliasing still works.
  * Vision §3.1 / Arch §3.1.2 — voice enabled only after consent-ack.
  * Vision §3.5 / Arch §3.4.3 — custom preset requires the 4 axes; businessContext ≤280.
  * Vision §3.4 / Arch §3.4.5 — the 4 escalation signals are fixed; tampering rejected.
  * Vision §3.2 / Arch §3.8.7 — builtin cognition is never an addon tool; unknown ids.
  * Arch §3.6 — config mutation blocked once grace-window/hard-deleted.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract]


async def _signup_verify_login(client) -> str:
    from app.auth import access

    email = f"inv-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return email


async def _create(client) -> None:
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )


# --- Invariant 5: unknown/extra fields rejected on every pillar (§6, Arch §3.4.3) --- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_model_selection_field_rejected_on_personality(client):
    await _signup_verify_login(client)
    await _create(client)
    # A model-selection field must NEVER be accepted (Arch §3.4.3) — extra='forbid' → 422.
    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={"preset": "professional_advisor", "model": "gpt-4o"},
    )
    assert r.status_code == 422
    # Axis10-F02: the 422 body must match the wire error contract {message,…} — NOT
    # FastAPI's native {detail:[…]} — so transport.ts surfaces a real message.
    body = r.json()
    assert "message" in body and "detail" not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unknown_field_rejected_on_every_pillar(client):
    await _signup_verify_login(client)
    await _create(client)
    # Each pillar body forbids extras (the structural "no second Connections mechanism"
    # and "no model selection" enforcement, §6).
    cases = [
        ("/api/v1/admin/luciel/channels", {"channels": [], "connections": []}),
        ("/api/v1/admin/luciel/tools", {"tools": [], "extraSurface": True}),
        ("/api/v1/admin/luciel/escalation", {"primaryEmail": "a@x.com", "signals": []}),
        ("/api/v1/admin/luciel/personality", {"preset": "warm_concierge", "llm": "x"}),
    ]
    for path, body in cases:
        r = await client.put(path, json=body)
        assert r.status_code == 422, f"{path} accepted an unknown field"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_camelcase_aliasing_still_works_under_forbid(client):
    await _signup_verify_login(client)
    await _create(client)
    # extra='forbid' must NOT break camelCase aliasing: ccOwnerEmail is a declared alias,
    # so the body validates and round-trips.
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={
            "primaryEmail": "sarah@x.com",
            "routing": [{"signal": "high_value_lead", "channel": "email", "ccOwnerEmail": True}],
        },
    )
    assert r.status_code == 200
    assert r.json()["escalation"]["routing"][0]["ccOwnerEmail"] is True


# --- Invariant 3: voice enabled only after consent-ack (Vision §3.1, Arch §3.1.2) --- #


def _channels_payload(*, voice_enabled: bool) -> dict:
    return {
        "channels": [
            {"id": "widget", "enabled": True},
            {"id": "voice", "enabled": voice_enabled, "connectionStatus": "connected"},
        ]
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_enabling_voice_without_consent_rejected_then_allowed_after_consent(client):
    await _signup_verify_login(client)
    await _create(client)
    path = "/api/v1/admin/luciel/channels"
    # Before consent: enabling voice is a 409 conflict (one-time consent-ack gate).
    r1 = await client.put(path, json=_channels_payload(voice_enabled=True))
    assert r1.status_code == 409

    # Acknowledge consent, then the same mutation succeeds.
    rc = await client.post("/api/v1/admin/luciel/voice-consent")
    assert rc.status_code == 200
    r2 = await client.put(path, json=_channels_payload(voice_enabled=True))
    assert r2.status_code == 200
    voice = next(c for c in r2.json()["channels"] if c["id"] == "voice")
    assert voice["enabled"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_voice_disabled_does_not_require_consent(client):
    await _signup_verify_login(client)
    await _create(client)
    # Leaving voice disabled never trips the gate.
    r = await client.put(
        "/api/v1/admin/luciel/channels", json=_channels_payload(voice_enabled=False)
    )
    assert r.status_code == 200


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unknown_channel_id_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    # ChannelIdWire pins the 6 frozen ids → unknown is a 422 (Vision §3.1).
    r = await client.put(
        "/api/v1/admin/luciel/channels",
        json={"channels": [{"id": "telegram", "enabled": True}]},
    )
    assert r.status_code == 422


# --- Invariant 2: personality (Vision §3.5, Arch §3.4.3) --- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_custom_preset_without_axes_rejected_then_allowed_with_axes(client):
    await _signup_verify_login(client)
    await _create(client)
    # custom preset REQUIRES the 4 axes → 422 when missing.
    r1 = await client.put("/api/v1/admin/luciel/personality", json={"preset": "custom"})
    assert r1.status_code == 422
    # With all four axes it succeeds.
    r2 = await client.put(
        "/api/v1/admin/luciel/personality",
        json={
            "preset": "custom",
            "axes": {"tone": 0.5, "verbosity": 0.5, "formality": 0.5, "pace": 0.5},
        },
    )
    assert r2.status_code == 200
    assert r2.json()["personality"]["preset"] == "custom"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_business_context_over_280_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={"preset": "warm_concierge", "businessContext": "x" * 281},
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_axis_out_of_range_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={
            "preset": "custom",
            "axes": {"tone": 1.5, "verbosity": 0.5, "formality": 0.5, "pace": 0.5},
        },
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_non_custom_preset_drops_axes(client):
    await _signup_verify_login(client)
    await _create(client)
    # A non-custom preset ignores axes per doc — they are not persisted.
    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={
            "preset": "warm_concierge",
            "axes": {"tone": 0.1, "verbosity": 0.1, "formality": 0.1, "pace": 0.1},
        },
    )
    assert r.status_code == 200
    assert r.json()["personality"].get("axes") is None


# --- Invariant 1: escalation 4 signals fixed (Vision §3.4, Arch §3.4.5) --- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_escalation_unknown_signal_in_routing_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    # A routing rule referencing a non-fixed signal is a 422 (signals are locked).
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"routing": [{"signal": "made_up_signal", "channel": "email"}]},
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_escalation_legal_routing_over_fixed_signals_allowed(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={
            "primaryEmail": "sarah@x.com",
            "preferredChannel": "email",
            "routing": [
                {"signal": "explicit_human_request", "channel": "email"},
                {"signal": "strong_negative_sentiment", "channel": "sms"},
                {"signal": "cannot_answer", "channel": "email"},
                {"signal": "high_value_lead", "channel": "email", "ccOwnerEmail": True},
            ],
        },
    )
    assert r.status_code == 200
    assert len(r.json()["escalation"]["routing"]) == 4


# --- Invariant 4: tools — builtin cognition never an addon; unknown ids (§3.2, §3.8.7) -- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_builtin_cognition_as_addon_tool_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    # capture_leads/escalate/handoff/summarize are non-interactive cognition, NOT addon
    # tools — naming one is a 422 (Arch §3.8.7).
    for cognition in ("capture_leads", "escalate", "handoff", "summarize"):
        r = await client.put(
            "/api/v1/admin/luciel/tools",
            json={"tools": [{"id": cognition, "enabled": True}]},
        )
        assert r.status_code == 422, f"{cognition} accepted as an addon tool"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unknown_addon_tool_id_rejected(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put(
        "/api/v1/admin/luciel/tools",
        json={"tools": [{"id": "mine_bitcoin", "enabled": True}]},
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_addon_tool_enabled_persists_even_if_connection_unhealthy(client):
    await _signup_verify_login(client)
    await _create(client)
    # The SECOND GATE (usable only when connection healthy) is RUNTIME, not config — so
    # config stores enabled=true even with an unhealthy connection (Arch §3.8.7).
    r = await client.put(
        "/api/v1/admin/luciel/tools",
        json={"tools": [{"id": "send_email", "enabled": True, "connectionStatus": "error"}]},
    )
    assert r.status_code == 200
    tool = next(t for t in r.json()["tools"] if t["id"] == "send_email")
    assert tool["enabled"] is True


# --- Invariant 6: lifecycle state gating (Arch §3.6) --- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_config_mutation_blocked_in_grace_window(client):
    await _signup_verify_login(client)
    await _create(client)
    # Soft-delete → grace window; config edits are then blocked (409 conflict).
    rd = await client.post("/api/v1/admin/luciel/delete")
    assert rd.json()["state"] == "luciel_grace_window"

    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={"preset": "professional_advisor"},
    )
    assert r.status_code == 409
    # voice-consent is gated the same way.
    rc = await client.post("/api/v1/admin/luciel/voice-consent")
    assert rc.status_code == 409


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_config_mutation_allowed_while_paused(client):
    await _signup_verify_login(client)
    await _create(client)
    # paused is non-destructive and instantly reversible — config edits remain allowed.
    await client.post("/api/v1/admin/luciel/pause")
    r = await client.put(
        "/api/v1/admin/luciel/personality",
        json={"preset": "friendly_expert"},
    )
    assert r.status_code == 200
    assert r.json()["personality"]["preset"] == "friendly_expert"


# --- the lead auto-prune rule is OPT-IN and off by default (§3.4.10a, Legal §B5) --- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lead_retention_is_off_until_the_admin_sets_it(client):
    """Off is the DEFAULT, and it is the only value the platform assumes on the Admin's
    behalf — captured Leads are their business data, never on a platform timer."""
    await _signup_verify_login(client)
    await _create(client)
    r = await client.get("/api/v1/admin/luciel")
    assert r.status_code == 200
    assert r.json()["leadRetentionDays"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lead_retention_round_trips_and_can_be_turned_back_off(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put("/api/v1/admin/luciel/lead-retention", json={"leadRetentionDays": 180})
    assert r.status_code == 200
    assert r.json()["leadRetentionDays"] == 180
    assert (await client.get("/api/v1/admin/luciel")).json()["leadRetentionDays"] == 180

    # The rule must be reversible as a setting even though its deletions are not.
    off = await client.put("/api/v1/admin/luciel/lead-retention", json={"leadRetentionDays": None})
    assert off.json()["leadRetentionDays"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_zero_or_negative_retention_window_is_rejected(client):
    """A 0-day window means "delete every lead the moment it stops being active", which no
    Admin means to ask for — ``null`` is how you turn the rule off."""
    await _signup_verify_login(client)
    await _create(client)
    for days in (0, -1):
        r = await client.put(
            "/api/v1/admin/luciel/lead-retention", json={"leadRetentionDays": days}
        )
        assert r.status_code == 422, f"accepted leadRetentionDays={days}"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lead_retention_forbids_unknown_fields_like_every_pillar(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.put(
        "/api/v1/admin/luciel/lead-retention",
        json={"leadRetentionDays": 90, "purgeArchived": True},
    )
    assert r.status_code == 422


# --- Round 6 WP-I (F066): the websites the widget may load on ---------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_allowed_origins_round_trip_normalise_and_clear(client):
    await _signup_verify_login(client)
    await _create(client)
    assert (await client.get("/api/v1/admin/luciel")).json()["allowedOrigins"] is None
    r = await client.put(
        "/api/v1/admin/luciel/allowed-origins",
        json={
            "origins": [
                "https://Acme.Example/",
                "https://acme.example:443",
                "http://localhost:3000",
            ]
        },
    )
    assert r.status_code == 200, r.text
    # Case-folded, default port dropped, duplicates collapsed, order kept.
    assert r.json()["allowedOrigins"] == ["https://acme.example", "http://localhost:3000"]
    assert (await client.get("/api/v1/admin/luciel")).json()["allowedOrigins"] == [
        "https://acme.example",
        "http://localhost:3000",
    ]
    off = await client.put("/api/v1/admin/luciel/allowed-origins", json={"origins": []})
    assert off.status_code == 200
    assert off.json()["allowedOrigins"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_allowed_origins_refuse_paths_schemes_and_too_many(client):
    """An origin with a path would silently never match and the owner's own site would be
    refused: it is rejected with the fix in the reason, never stored."""
    await _signup_verify_login(client)
    await _create(client)
    for bad in (
        ["https://acme.example/pricing"],
        ["ftp://acme.example"],
        ["acme.example"],
        ["https://user:pw@acme.example"],
        ["https://acme.example?x=1"],
        [f"https://site{i}.example" for i in range(11)],
    ):
        r = await client.put("/api/v1/admin/luciel/allowed-origins", json={"origins": bad})
        assert r.status_code == 422, bad
    r = await client.put(
        "/api/v1/admin/luciel/allowed-origins",
        json={"origins": ["https://acme.example"], "strict": True},
    )
    assert r.status_code == 422  # unknown fields refused like every pillar
