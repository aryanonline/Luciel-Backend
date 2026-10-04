"""The owner-facing audit log carries a human detail (round 7 WP-5, item 6).

Live condition: ``GET /admin/usage/audit-log`` never populated ``detail`` from
``event_payload``, so the owner saw "lifecycle_transition" fifty times with no way to
tell a pause from a delete, and "data_retention_hard_delete" with no data class or
count. The detail is rendered from a per-event allow-list of CODE-shaped payload
values only — never a secret, never a value that is PII, never a provider's sentence.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.admin.usage import audit_detail
from app.audit import log as audit
from app.db.session import tenant_session
from app.models.audit import ActorType

pytestmark = pytest.mark.contract


async def _login(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"audit-{uuid.uuid4().hex[:8]}@x.com"
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


def test_lifecycle_and_retention_events_render_from_their_payload() -> None:
    assert audit_detail("lifecycle_transition", {"from": "active", "to": "paused"}) == (
        "active → paused"
    )
    assert (
        audit_detail(
            "lifecycle_transition",
            {
                "from": "luciel_grace_window",
                "to": "luciel_hard_deleted",
                "trigger": "grace_window_expiry",
            },
        )
        == "luciel_grace_window → luciel_hard_deleted (grace_window_expiry)"
    )
    assert (
        audit_detail(
            "data_retention_hard_delete",
            {
                "data_class": "transcript",
                "count": 3,
                "retention_policy_applied": "age_based_sweep:365d",
            },
        )
        == "transcript: 3 deleted (age_based_sweep:365d)"
    )
    assert (
        audit_detail(
            "data_retention_hard_delete",
            {"data_class": "lead_record", "retention_policy_applied": "luciel_hard_delete_cascade"},
        )
        == "lead_record (luciel_hard_delete_cascade)"
    )


def test_billing_and_connection_events_render_from_their_payload() -> None:
    assert (
        audit_detail(
            "dunning_state_changed", {"from": "none", "to": "retrying", "reason": "payment_failed"}
        )
        == "none → retrying (payment_failed)"
    )
    assert audit_detail("lifecycle_transition", {"from": "free_cap", "to": "payg_enabled"}) == (
        "free_cap → payg_enabled"
    )
    assert audit_detail("payment_method_added", {"brand": "visa", "last4": "4242"}) == (
        "visa card added"
    )
    assert audit_detail("payment_method_removed", {"reason": "admin_removed"}) == (
        "card removed (admin_removed)"
    )
    assert (
        audit_detail(
            "connection_status_changed",
            {
                "connection_id": str(uuid.uuid4()),
                "old_status": "connected",
                "new_status": "expired",
                "reason": "health_check",
            },
        )
        == "connected → expired (health_check)"
    )
    assert (
        audit_detail(
            "connection_status_changed",
            {
                "action": "deferred_abandoned",
                "action_type": "schedule_callback",
                "connection_type": "email_sender",
                "reason": "undeliverable > 24h TTL",
            },
        )
        == "deferred_abandoned: schedule_callback on email_sender"
    )


def test_the_detail_never_carries_a_sentence_a_secret_or_pii() -> None:
    """Only code-shaped values from the allow-list are rendered: a status_detail that
    quotes a provider (which can carry a phone number or an address) is dropped, and an
    event type with no renderer has no detail at all, whatever its payload holds."""
    assert (
        audit_detail(
            "connection_status_changed",
            {
                "old_status": "connected",
                "new_status": "error",
                "reason": "Token refresh failed for +1 555 010 9999, call the owner",
            },
        )
        == "connected → error"
    )
    assert (
        audit_detail(
            "escalation_notification_sent", {"recipient": "owner@acme.example", "channel": "email"}
        )
        is None
    )
    assert audit_detail("login_succeeded", {}) is None
    assert audit_detail("lifecycle_transition", {"from": "active"}) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_audit_log_route_serves_the_detail(client):
    admin_id = await _login(client)
    async with tenant_session(admin_id) as s:
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            actor_user_id=admin_id,
            event_type="lifecycle_transition",
            payload={"from": "active", "to": "luciel_grace_window"},
        )
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "session_summary",
                "count": 12,
                "retention_policy_applied": "age_based_sweep:365d",
            },
        )
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(uuid.uuid4()),
                "old_status": "connected",
                "new_status": "expired",
                "reason": "refresh token rejected by provider for mailbox jane@acme.example",
            },
        )

    r = await client.get("/api/v1/admin/usage/audit-log")
    assert r.status_code == 200
    by_type = {row["eventType"]: row for row in r.json()}
    assert by_type["lifecycle_transition"]["detail"] == "active → luciel_grace_window"
    assert by_type["data_retention_hard_delete"]["detail"] == (
        "session_summary: 12 deleted (age_based_sweep:365d)"
    )
    assert by_type["connection_status_changed"]["detail"] == "connected → expired"
    assert "jane@acme.example" not in r.text
    # Credential-lifecycle rows (login/verify) carry no renderable payload: detail null.
    assert by_type["login_succeeded"]["detail"] is None
