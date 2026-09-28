"""Round 7 WP-4a — BYO connection lifecycle and health truth.

Service-layer proofs (no HTTP) for the live conditions the round-7 audit verified:
a number attach must not silence an OAuth Twilio row's probe; the sweep probes the
operable-but-pending sms statuses without ever promoting them; an abandoned reconnect
marker is cleared instead of excluding the row from health forever; re-verify's status
writes are audited like every other transition; a serving row is never reset in place
by a second Connect; a credential-form swap proves its target before the live secret
moves; a disconnect releases the routing claim and forgets CRM state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.channels.number_operability import OperabilityResult
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ACCOUNT_SID = "AC" + "4" * 32


async def _seed_tenant(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
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
                id=iid,
                admin_id=admin,
                name="Acme",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


async def _seed_connection(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    connection_type: str,
    provider: str,
    auth_class: str,
    status: str,
    detail: str | None = None,
    cfg: dict[str, object] | None = None,
    secret_ref: str | None = None,
) -> uuid.UUID:
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                auth_class=auth_class,
                provider=provider,
                status=status,
                status_detail=detail,
                secret_ref=secret_ref,
                non_secret_config=dict(cfg or {}),
            )
        )
    return cid


async def _row(admin: uuid.UUID, cid: uuid.UUID) -> InstanceConnection:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()


async def _status_changes(admin: uuid.UUID) -> list[dict[str, object]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_status_changed",
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload or {}) for r in rows]


class _FixedProbe:
    def __init__(self, result: OperabilityResult) -> None:
        self._result = result

    async def check(self, e164_number: str) -> OperabilityResult:
        return self._result


# --------------------------------------------------------------------------- #
# Item 2 — a number attach keeps the OAuth class, so the bearer probe keeps running
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_oauth_twilio_row_keeps_its_class_through_a_number_attach_and_is_probed():
    from app.channels.phone_lifecycle import attach_sms_number
    from app.connections.probes import liveness_request_for

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/sms_sender"
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="oauth_token",
        status="unconfigured",
        cfg={
            "accountSid": ACCOUNT_SID,
            "mode": "oauth",
            "token_expires_at": "2030-01-01T00:00:00+00:00",
        },
        secret_ref=ref,
    )

    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number="+14155550123",
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        assert conn.status == "pending_carrier_registration"
        assert conn.auth_class == "oauth_token", "the attach must not re-stamp an OAuth row"

    row = await _row(admin, cid)
    assert row.auth_class == "oauth_token"
    # The sweep's liveness call is the bearer GET /Accounts — it was None (never
    # probed again) once the row had been stamped api_key with no key material.
    request = liveness_request_for(row, {"access_token": "tw-bearer-1"})
    assert request is not None
    assert request.headers.get("Authorization") == "Bearer tw-bearer-1"
    assert request.auth is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_credential_form_twilio_row_stays_api_key_through_a_number_attach():
    from app.channels.phone_lifecycle import attach_sms_number

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="unconfigured",
        cfg={"accountSid": ACCOUNT_SID},
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number="+14155550124",
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        assert conn.auth_class == "api_key"


# --------------------------------------------------------------------------- #
# Item 3 — the sweep PROBES pending/not-operable sms rows but never promotes them
# --------------------------------------------------------------------------- #

PROBE_ONLY = ("pending_carrier_registration", "not_operable_hosting_required")


async def _sweep(admin: uuid.UUID, now: datetime = NOW) -> dict[str, str]:
    from app.connections.health import ReconnectOnExpiry, StoredCredentialProbe, run_health_sweep

    async with tenant_session(admin) as s:
        return await run_health_sweep(
            s,
            admin_id=admin,
            probe=StoredCredentialProbe(),
            refresher=ReconnectOnExpiry(),
            now=now,
        )


async def _attention_emails(admin: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_attention_emailed",
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_sending_pending_registration_row_with_a_dead_credential_flips_to_error():
    """The live shape: a text can leave on a pending_carrier_registration row
    (outbound.SMS_OPERABLE_STATUSES), but the sweep never re-checked its credential,
    so a Twilio grant gone at the provider stayed "pending registration" while every
    send failed in front of a customer."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="pending_carrier_registration",
        detail="registration guidance",
        cfg={"accountSid": ACCOUNT_SID, "destination": "+14155550130"},
        secret_ref=f"vm/{admin}/{iid}/sms_sender",  # stamped, nothing behind it
    )
    result = await _sweep(admin)
    assert result.get(str(cid)) == "error"
    row = await _row(admin, cid)
    assert row.status == "error"
    assert "reconnect" in (row.status_detail or "")
    assert row.last_health_check_at == NOW
    assert await _attention_emails(admin) == 1
    changes = await _status_changes(admin)
    assert [(c["old_status"], c["new_status"]) for c in changes] == [
        ("pending_carrier_registration", "error")
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", PROBE_ONLY)
async def test_a_healthy_probe_only_row_is_stamped_but_never_promoted(status: str):
    """A credential Twilio still accepts is not carrier registration and not a hosted
    number: the setup-owned status and its guidance stay, only the check is stamped."""
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/sms_sender"
    await get_secret_store().put(ref, {"authToken": "tok"})
    detail = "setup guidance the owner still needs"
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status=status,
        detail=detail,
        cfg={"accountSid": ACCOUNT_SID, "pending_destination": "+14155550131"},
        secret_ref=ref,
    )
    result = await _sweep(admin)
    assert result.get(str(cid)) == status, "probed — and still its own status"
    row = await _row(admin, cid)
    assert row.status == status
    assert row.status_detail == detail
    assert row.last_health_check_at == NOW
    assert await _attention_emails(admin) == 0
    assert await _status_changes(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_live_refusal_flips_a_pending_registration_row_at_once():
    """A 401 on a real send (health.flag_if_provider_auth_failure) reaches the
    probe-only row too, instead of being left to an owner who cannot see it."""
    from app.connections import health

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="pending_carrier_registration",
        cfg={"accountSid": ACCOUNT_SID, "destination": "+14155550132"},
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        flipped = await health.flag_if_provider_auth_failure(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="sms_sender",
            reason="twilio_send_http_401",
        )
        assert flipped is True
    row = await _row(admin, cid)
    assert row.status == "error"
    assert await _attention_emails(admin) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_repaired_credential_returns_a_parked_number_to_its_honest_state():
    """After the sweep flipped a not-operable row to error, the owner's fixed
    credential must not leave the row reading "error" (a credential problem they
    just repaired) — the number's truth is unchanged: still not operable, still the
    hosting guidance."""
    from app.connections import service as conn_service

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="error",
        detail="Your Twilio account rejected the stored credential (HTTP 401) — reconnect",
        cfg={"accountSid": ACCOUNT_SID, "pending_destination": "+14155550133"},
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        conn = await conn_service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=cid,
            fields={"accountSid": ACCOUNT_SID, "authToken": "fresh-token"},
            user_id=admin,
        )
        assert conn is not None
        assert conn.status == "not_operable_hosting_required"
        assert "Re-verify" in (conn.status_detail or "")
    row = await _row(admin, cid)
    assert row.status == "not_operable_hosting_required"
    assert (row.non_secret_config or {}).get("pending_destination") == "+14155550133"


# --------------------------------------------------------------------------- #
# Item 9 — re-verify and attach write status through the shared owner transition
# --------------------------------------------------------------------------- #


async def _status_change_rows(admin: uuid.UUID) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_status_changed",
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reverify_demotion_from_connected_is_audited_under_the_owner_without_an_email():
    """A re-verify that finds the attested, texting-enabled number no longer operable
    switches texting off — the live condition that emitted no audit row at all."""
    from app.channels.phone_lifecycle import reverify_sms_number
    from app.models.audit import ActorType

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="connected",
        detail="attested",
        cfg={
            "accountSid": ACCOUNT_SID,
            "destination": "+14155550140",
            "carrier_registration_attested_at": "2026-09-01T00:00:00+00:00",
        },
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        conn = await reverify_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=_FixedProbe(OperabilityResult.NOT_OPERABLE),
            user_id=admin,
        )
        assert conn is not None
        assert conn.status == "not_operable_hosting_required"
    rows = await _status_change_rows(admin)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_type is ActorType.admin_user
    assert row.actor_user_id == admin
    payload = dict(row.event_payload or {})
    assert payload["connection_id"] == str(cid)
    assert (payload["old_status"], payload["new_status"]) == (
        "connected",
        "not_operable_hosting_required",
    )
    assert payload["reason"] == "sms_number_reverified"
    # Owner-initiated: the sweep's attention email never fires here.
    assert await _attention_emails(admin) == 0
    assert (await _row(admin, cid)).last_health_check_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reverify_that_changes_nothing_writes_no_audit_row():
    from app.channels.phone_lifecycle import reverify_sms_number

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="pending_carrier_registration",
        cfg={"accountSid": ACCOUNT_SID, "destination": "+14155550141"},
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        conn = await reverify_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=_FixedProbe(OperabilityResult.OPERABLE),
            user_id=admin,
        )
        assert conn is not None and conn.status == "pending_carrier_registration"
    assert await _status_change_rows(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_number_change_that_demotes_a_serving_row_is_audited():
    """Attaching a number the account cannot operate over a connected row is a
    demotion the owner's audit page must show."""
    from app.channels.phone_lifecycle import attach_sms_number

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="connected",
        cfg={
            "accountSid": ACCOUNT_SID,
            "destination": "+14155550142",
            "carrier_registration_attested_at": "2026-09-01T00:00:00+00:00",
        },
        secret_ref=f"vm/{admin}/{iid}/sms_sender",
    )
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number="+14155550143",
            probe=_FixedProbe(OperabilityResult.NOT_OPERABLE),
            user_id=admin,
        )
        assert conn.status == "not_operable_hosting_required"
    changes = await _status_changes(admin)
    assert changes == [
        {
            "connection_id": str(cid),
            "old_status": "connected",
            "new_status": "not_operable_hosting_required",
            "reason": "sms_number_attached",
        }
    ]
    assert await _attention_emails(admin) == 0


# --------------------------------------------------------------------------- #
# Item 5 — an abandoned reconnect marker stops shielding the row after 15 minutes
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_abandoned_swap_is_cleared_after_fifteen_minutes_and_the_row_is_swept():
    from datetime import timedelta

    from app.connections import service as conn_service
    from app.connections.secrets import get_secret_store
    from app.knowledge.providers import staged_secret_ref

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "live-tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        out = await conn_service.swap(
            s,
            admin_id=admin,
            connection_id=cid,
            provider="salesforce",
            user_id=admin,
            now=NOW,
        )
        assert out is not None
    # A consent the owner started but never finished left a staged token behind.
    await get_secret_store().put(staged_secret_ref(ref), {"access_token": "half-done"})

    row = await _row(admin, cid)
    cfg = row.non_secret_config or {}
    assert cfg.get("pending_provider") == "salesforce"
    assert cfg.get("reconnect_pending_since") == NOW.isoformat()
    assert (row.status_detail or "").startswith("reconnect_pending:prior_status=connected")

    # Ten minutes in: the attempt is still live — the marker shields the row.
    result = await _sweep(admin, now=NOW + timedelta(minutes=10))
    assert result.get(str(cid)) == "connected"
    row = await _row(admin, cid)
    assert (row.status_detail or "").startswith("reconnect_pending:")
    assert row.last_health_check_at is None
    assert (row.non_secret_config or {}).get("pending_provider") == "salesforce"

    # Sixteen minutes in: abandoned — cleared, restored, audited, then probed.
    later = NOW + timedelta(minutes=16)
    result = await _sweep(admin, now=later)
    assert result.get(str(cid)) == "connected"
    row = await _row(admin, cid)
    assert row.status == "connected"
    assert row.status_detail is None
    assert row.provider == "hubspot", "the live provider was never cut over"
    assert row.last_health_check_at == later
    cfg = row.non_secret_config or {}
    assert "pending_provider" not in cfg
    assert "reconnect_pending_since" not in cfg
    assert "oauth_state_jti" not in cfg
    assert await get_secret_store().get(staged_secret_ref(ref)) is None
    assert await get_secret_store().get(ref) == {"access_token": "live-tok"}
    reasons = [c["reason"] for c in await _status_changes(admin)]
    assert "reconnect_abandoned" in reasons


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_marker_without_a_timestamp_is_abandoned_and_a_dead_credential_is_caught():
    """The pre-item-5 shape: a marker nothing could ever clear, over a credential
    that has since vanished — the row read Connected forever."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="calendar",
        provider="google_calendar",
        auth_class="oauth_token",
        status="connected",
        detail="reconnect_pending:prior_status=connected",
        cfg={"pending_provider": "calendly", "oauth_state_jti": "stale"},
        secret_ref=f"vm/{admin}/{iid}/calendar",  # stamped, nothing behind it
    )
    result = await _sweep(admin)
    assert result.get(str(cid)) == "error"
    row = await _row(admin, cid)
    assert row.status == "error"
    assert not (row.status_detail or "").startswith("reconnect_pending:")
    assert row.provider == "google_calendar"
    assert "pending_provider" not in (row.non_secret_config or {})
    assert "oauth_state_jti" not in (row.non_secret_config or {})
    assert await _attention_emails(admin) == 1
    transitions = [
        (c["old_status"], c["new_status"], c["reason"]) for c in await _status_changes(admin)
    ]
    assert transitions[0] == ("connected", "connected", "reconnect_abandoned")
    assert transitions[-1][:2] == ("connected", "error")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_abandoned_reconnect_on_a_pending_number_restores_its_guidance():
    from datetime import timedelta

    from app.connections import service as conn_service
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/sms_sender"
    await get_secret_store().put(ref, {"authToken": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="pending_carrier_registration",
        detail="registration guidance",
        cfg={"accountSid": ACCOUNT_SID, "destination": "+14155550150"},
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        await conn_service.reconnect(s, admin_id=admin, connection_id=cid, user_id=admin, now=NOW)
    await _sweep(admin, now=NOW + timedelta(minutes=20))
    row = await _row(admin, cid)
    assert row.status == "pending_carrier_registration"
    assert "A2P 10DLC" in (row.status_detail or ""), "the number guidance is back, not blank"


# --------------------------------------------------------------------------- #
# Item 4 — a credential-form swap proves its target before the live secret moves
# --------------------------------------------------------------------------- #

WEBHOOK_URL = "https://hooks.acme.example/leads"


@pytest.fixture
def _record_transport():
    from app.tools.record_backend import MockRecordTransport

    MockRecordTransport.reset()
    yield MockRecordTransport
    MockRecordTransport.reset()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hubspot_to_webhook_swap_whose_endpoint_refuses_leaves_hubspot_serving(
    _record_transport,
):
    from app.connections import service as conn_service
    from app.connections.secrets import get_secret_store
    from app.knowledge.providers import staged_secret_ref

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "hs-live"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        out = await conn_service.swap(
            s, admin_id=admin, connection_id=cid, provider="custom_webhook", user_id=admin, now=NOW
        )
        assert out is not None and out[1] == {"requires_client_form": True}

    # The owner's endpoint refuses the header they pasted.
    _record_transport.seed(WEBHOOK_URL, 401)
    with pytest.raises(conn_service.CredentialRejected, match="refused the Authorization header"):
        async with tenant_session(admin) as s:
            await conn_service.submit_credentials(
                s,
                admin_id=admin,
                connection_id=cid,
                fields={"url": WEBHOOK_URL, "auth_header": "Bearer wrong"},
                user_id=admin,
            )
    row = await _row(admin, cid)
    assert row.provider == "hubspot"
    assert row.auth_class == "oauth_token"
    assert row.status == "connected"
    assert "url" not in (row.non_secret_config or {})
    assert await get_secret_store().get(ref) == {"access_token": "hs-live"}, "HubSpot still serves"
    assert await get_secret_store().get(staged_secret_ref(ref)) is None, "the refused copy is gone"
    # The swap is still open for a corrected submit (item 5 abandons it in 15 minutes).
    assert (row.non_secret_config or {}).get("pending_provider") == "custom_webhook"
    assert (row.status_detail or "").startswith("reconnect_pending:prior_status=connected")

    # A corrected header the endpoint accepts (a POST-only receiver answers 405 to
    # the GET — reachable, header accepted) promotes: cutover happens only now.
    _record_transport.seed(WEBHOOK_URL, 405)
    async with tenant_session(admin) as s:
        conn = await conn_service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=cid,
            fields={"url": WEBHOOK_URL, "auth_header": "Bearer right"},
            user_id=admin,
        )
        assert conn is not None
    row = await _row(admin, cid)
    assert row.provider == "custom_webhook"
    assert row.auth_class == "long_lived_token"
    assert row.status == "connected"
    assert row.status_detail is None
    assert (row.non_secret_config or {}).get("url") == WEBHOOK_URL
    assert "pending_provider" not in (row.non_secret_config or {})
    assert await get_secret_store().get(ref) == {"auth_header": "Bearer right"}
    assert await get_secret_store().get(staged_secret_ref(ref)) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_down_webhook_endpoint_fails_a_fresh_connect_without_a_connected_row(
    _record_transport,
):
    from app.connections import service as conn_service
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/outbound_webhook"
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="outbound_webhook",
        provider="outbound_webhook",
        auth_class="long_lived_token",
        status="unconfigured",
        secret_ref=ref,
    )
    _record_transport.seed(WEBHOOK_URL, 503)
    with pytest.raises(conn_service.CredentialRejected, match="did not answer"):
        async with tenant_session(admin) as s:
            await conn_service.submit_credentials(
                s,
                admin_id=admin,
                connection_id=cid,
                fields={"url": WEBHOOK_URL},
                user_id=admin,
            )
    row = await _row(admin, cid)
    assert row.status == "unconfigured"
    assert await get_secret_store().get(ref) is None
