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


# --------------------------------------------------------------------------- #
# Item 6 — a webhook endpoint that refuses the owner's header flips the row at once
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_webhook_crm_that_refuses_the_header_on_a_push_flips_the_row(monkeypatch):
    from app.connections import sandbox
    from app.connections.secrets import get_secret_store
    from app.tools import contract as c
    from app.tools.contract import ToolContext
    from app.tools.crm_backend import CrmUnavailable
    from app.tools.registry import _TenantCrmBackend

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"auth_header": "Bearer revoked"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="custom_webhook",
        auth_class="long_lived_token",
        status="connected",
        cfg={"url": WEBHOOK_URL, "egressAllowlist": ["hooks.acme.example"]},
        secret_ref=ref,
    )

    async def _refused(**kwargs):  # noqa: ANN003 — mirrors run_byo_webhook's kwargs
        return c.failed("non_2xx:401")

    monkeypatch.setattr(sandbox, "run_byo_webhook", _refused)
    # The push wrapper (runtime.crm_push) catches the failure INSIDE the turn's
    # session and stamps the lead, so the flag commits with it — mirrored here.
    async with tenant_session(admin) as s:
        with pytest.raises(CrmUnavailable, match="crm_webhook_non_2xx:401"):
            await _TenantCrmBackend().upsert_lead(
                ToolContext(admin_id=admin, instance_id=iid, session=s),
                "jane@acme.com",
                {"email": "jane@acme.com"},
            )
    row = await _row(admin, cid)
    assert row.status == "error"
    assert "Custom webhook rejected the stored credential" in (row.status_detail or "")
    assert "non_2xx:401" in (row.status_detail or "")
    transitions = [(x["old_status"], x["new_status"]) for x in await _status_changes(admin)]
    assert transitions == [("connected", "error")]
    assert await _attention_emails(admin) == 1

    # A plain outage (5xx) is not a refusal: the row stays connected.
    admin2 = uuid.uuid4()
    iid2 = await _seed_tenant(admin2)
    ref2 = f"vm/{admin2}/{iid2}/crm"
    await get_secret_store().put(ref2, {"auth_header": "Bearer fine"})
    cid2 = await _seed_connection(
        admin2,
        iid2,
        connection_type="crm",
        provider="custom_webhook",
        auth_class="long_lived_token",
        status="connected",
        cfg={"url": WEBHOOK_URL, "egressAllowlist": ["hooks.acme.example"]},
        secret_ref=ref2,
    )

    async def _down(**kwargs):  # noqa: ANN003
        return c.failed("non_2xx:503")

    monkeypatch.setattr(sandbox, "run_byo_webhook", _down)
    async with tenant_session(admin2) as s:
        with pytest.raises(CrmUnavailable, match="crm_webhook_non_2xx:503"):
            await _TenantCrmBackend().upsert_lead(
                ToolContext(admin_id=admin2, instance_id=iid2, session=s),
                "jane@acme.com",
                {"email": "jane@acme.com"},
            )
    assert (await _row(admin2, cid2)).status == "connected"


# --------------------------------------------------------------------------- #
# Item 8 — a second Connect never resets a serving row in place
# --------------------------------------------------------------------------- #


async def test_the_serving_statuses_cover_every_status_that_dispatches():
    """The refusal boundary must not drift from the two gates that actually serve:
    the universal gate's ``connected`` and the sms operability set."""
    from app.channels.outbound import SMS_OPERABLE_STATUSES
    from app.connections.gate import CONNECTED
    from app.connections.service import SERVING_STATUSES

    assert CONNECTED in SERVING_STATUSES
    assert SMS_OPERABLE_STATUSES <= SERVING_STATUSES


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    ("connection_type", "provider", "status", "new_provider"),
    [
        ("crm", "hubspot", "connected", "salesforce"),
        ("crm", "hubspot", "connected", "hubspot"),
        ("sms_sender", "twilio", "pending_carrier_registration", "twilio"),
    ],
)
async def test_a_second_connect_on_a_serving_row_is_refused_and_changes_nothing(
    connection_type: str, provider: str, status: str, new_provider: str
):
    """The live condition: an owner (or a stale dashboard tab) clicks Connect on a
    row that is serving. Before, the row was overwritten to unconfigured with the new
    provider and the secret_ref left pointing at material that no longer matched —
    the calendar went offline before any consent screen was even shown."""
    from app.connections import service
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/{connection_type}"
    await get_secret_store().put(ref, {"access_token": "live-tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type=connection_type,
        provider=provider,
        auth_class="oauth_token",
        status=status,
        cfg={"destination": "+14155550100"} if connection_type == "sms_sender" else {},
        secret_ref=ref,
    )

    async with tenant_session(admin) as s:
        with pytest.raises(service.ConnectionServing) as excinfo:
            await service.start_connection(
                s,
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                provider=new_provider,
                user_id=None,
            )
    assert str(excinfo.value).startswith("connection_serving_use_swap:")
    assert excinfo.value.connection_id == cid

    row = await _row(admin, cid)
    assert row.provider == provider
    assert row.status == status
    assert row.auth_class == "oauth_token"
    assert await get_secret_store().get(ref) == {"access_token": "live-tok"}
    assert await _status_changes(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", ["error", "expired", "unconfigured", "not_connected"])
async def test_a_provider_change_on_a_non_serving_row_destroys_the_old_secret(status: str):
    """A dead HubSpot row re-pointed at Salesforce must not carry the HubSpot token
    under the Salesforce name: the probe and the tools would read it as a Salesforce
    token. Live AND staged material go, and the cleanup is audited by ref only."""
    from app.connections import service
    from app.connections.secrets import get_secret_store
    from app.knowledge.providers import staged_secret_ref

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "hubspot-tok"})
    await get_secret_store().put(staged_secret_ref(ref), {"access_token": "half-staged"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status=status,
        detail="reconnect_pending:prior_status=error" if status == "error" else None,
        cfg={"pending_provider": "salesforce", "oauth_state_jti": "stale", "authorize_url": "x"},
        secret_ref=ref,
    )

    async with tenant_session(admin) as s:
        connection, result = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="salesforce",
            user_id=None,
        )
        assert connection.connection_id == cid
        assert "authorize_url" in result

    row = await _row(admin, cid)
    assert row.provider == "salesforce"
    assert row.status == "unconfigured"
    assert row.secret_ref == ref, "the ref is reused; only its material was destroyed"
    assert await get_secret_store().get(ref) is None
    assert await get_secret_store().get(staged_secret_ref(ref)) is None
    cfg = row.non_secret_config or {}
    assert "pending_provider" not in cfg
    # The parked state of the abandoned attempt was swept (a fresh one is minted only
    # once the platform OAuth app for the provider is configured).
    assert cfg.get("oauth_state_jti") != "stale"

    async with tenant_session(admin) as s:
        cleanups = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_secret_cleanup",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(cleanups) == 1
    payload = cleanups[0].event_payload or {}
    assert payload["reason"] == "provider_changed"
    assert payload["deleted"] is True
    assert payload["old_provider"] == "hubspot" and payload["new_provider"] == "salesforce"
    assert "hubspot-tok" not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_same_provider_reconnect_on_a_broken_row_keeps_its_secret():
    """Only a PROVIDER change destroys material: reconnecting the same provider on
    an error row leaves the credential for the callback to overwrite (and for the
    owner to cancel out of without losing anything that was there)."""
    from app.connections import service
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "hubspot-tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="error",
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="hubspot",
            user_id=None,
        )
    row = await _row(admin, cid)
    assert row.status == "unconfigured" and row.provider == "hubspot"
    assert await get_secret_store().get(ref) == {"access_token": "hubspot-tok"}
    async with tenant_session(admin) as s:
        cleanups = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_secret_cleanup",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert cleanups == []


# --------------------------------------------------------------------------- #
# Item 10 — a 429 from the provider is a backoff the next sweep honours
# --------------------------------------------------------------------------- #


class _AnswersProbe:
    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.calls = 0

    async def probe(self, connection: InstanceConnection) -> object:
        self.calls += 1
        return self._results.pop(0)


async def _sweep_with(admin: uuid.UUID, probe: object, now: datetime) -> dict[str, str]:
    from app.connections.health import ReconnectOnExpiry, run_health_sweep

    async with tenant_session(admin) as s:
        return await run_health_sweep(
            s, admin_id=admin, probe=probe, refresher=ReconnectOnExpiry(), now=now
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rate_limited_probe_persists_a_backoff_the_sweep_honours():
    """The live condition: HubSpot answers 429 to the identity call. Before, the
    sweep logged probe_skipped, stamped the row as checked, and asked again next
    sweep — nothing remembered the provider's Retry-After, so a tenant behind a
    rate limit was hammered on every pass and never actually verified. Now the
    backoff is persisted as ``next_probe_at`` and the sweep skips the row until it
    passes; the first answered probe afterwards clears it."""
    from datetime import timedelta

    from app.connections.health import NEXT_PROBE_AT_KEY, ProbeResult
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        secret_ref=ref,
    )

    limited = _AnswersProbe(
        ProbeResult(ok=True, detail="probe_skipped:http_429", retry_after=timedelta(hours=2))
    )
    assert await _sweep_with(admin, limited, NOW) == {str(cid): "connected"}
    row = await _row(admin, cid)
    assert row.status == "connected"
    assert (row.non_secret_config or {})[NEXT_PROBE_AT_KEY] == (
        NOW + timedelta(hours=2)
    ).isoformat()
    assert row.last_health_check_at is None, "a probe the provider refused to answer is not a check"

    # Inside the backoff the row is not asked at all — even a probe that would
    # declare it dead never runs.
    dead = _AnswersProbe(ProbeResult(ok=False, detail="dead"))
    assert await _sweep_with(admin, dead, NOW + timedelta(hours=1)) == {str(cid): "connected"}
    assert dead.calls == 0
    assert (await _row(admin, cid)).status == "connected"
    assert await _status_changes(admin) == []

    # Once the backoff passes the row is probed like any other: the hint is cleared
    # and the dead credential is caught with the usual audit + one attention email.
    assert await _sweep_with(admin, dead, NOW + timedelta(hours=3)) == {str(cid): "error"}
    assert dead.calls == 1
    row = await _row(admin, cid)
    assert row.status == "error"
    assert NEXT_PROBE_AT_KEY not in (row.non_secret_config or {})
    assert row.last_health_check_at == NOW + timedelta(hours=3)
    changes = await _status_changes(admin)
    assert [(c["old_status"], c["new_status"]) for c in changes] == [("connected", "error")]
    assert await _attention_emails(admin) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_answered_probe_clears_a_stale_backoff_hint():
    from datetime import timedelta

    from app.connections.health import NEXT_PROBE_AT_KEY, ProbeResult
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        cfg={NEXT_PROBE_AT_KEY: (NOW - timedelta(minutes=1)).isoformat(), "keep": "me"},
        secret_ref=ref,
    )
    healthy = _AnswersProbe(ProbeResult(ok=True))
    assert await _sweep_with(admin, healthy, NOW) == {str(cid): "connected"}
    assert healthy.calls == 1
    row = await _row(admin, cid)
    assert NEXT_PROBE_AT_KEY not in (row.non_secret_config or {})
    assert (row.non_secret_config or {}).get("keep") == "me"
    assert row.last_health_check_at == NOW


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_malformed_backoff_hint_never_shields_a_row():
    from app.connections.health import NEXT_PROBE_AT_KEY, ProbeResult
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        cfg={NEXT_PROBE_AT_KEY: "not-a-timestamp"},
        secret_ref=ref,
    )
    dead = _AnswersProbe(ProbeResult(ok=False, detail="dead"))
    assert await _sweep_with(admin, dead, NOW) == {str(cid): "error"}
    assert NEXT_PROBE_AT_KEY not in ((await _row(admin, cid)).non_secret_config or {})


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replaced_credential_does_not_pass_its_backoff_to_the_new_one(_record_transport):
    """The wait was earned by the credential the provider rate-limited. A swap that
    is confirmed, or a credential form that is proven and promoted, starts the new
    credential with no wait — while a FAILED confirm leaves the old credential
    serving with its backoff intact."""
    from datetime import timedelta

    from app.connections import service as conn_service
    from app.connections.health import NEXT_PROBE_AT_KEY
    from app.connections.secrets import get_secret_store

    hint = (NOW + timedelta(hours=6)).isoformat()
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        cfg={NEXT_PROBE_AT_KEY: hint},
        secret_ref=ref,
    )

    # A reconnect whose new credential fails its check: the old one keeps its wait.
    async with tenant_session(admin) as s:
        await conn_service.swap(
            s, admin_id=admin, connection_id=cid, provider="salesforce", user_id=admin, now=NOW
        )
        ok, _ = await conn_service.confirm_reconnect(
            s, admin_id=admin, connection_id=cid, user_id=admin, probe_ok=False
        )
        assert ok is False
    row = await _row(admin, cid)
    assert row.provider == "hubspot"
    assert (row.non_secret_config or {})[NEXT_PROBE_AT_KEY] == hint

    # A confirmed swap: the new credential starts clean.
    async with tenant_session(admin) as s:
        await conn_service.swap(
            s, admin_id=admin, connection_id=cid, provider="salesforce", user_id=admin, now=NOW
        )
        ok, _ = await conn_service.confirm_reconnect(
            s, admin_id=admin, connection_id=cid, user_id=admin, probe_ok=True
        )
        assert ok is True
    row = await _row(admin, cid)
    assert row.provider == "salesforce"
    assert NEXT_PROBE_AT_KEY not in (row.non_secret_config or {})

    # A proven credential-form submit on a row carrying a hint: same.
    hook_ref = f"vm/{admin}/{iid}/outbound_webhook"
    hook = await _seed_connection(
        admin,
        iid,
        connection_type="outbound_webhook",
        provider="outbound_webhook",
        auth_class="long_lived_token",
        status="error",
        cfg={NEXT_PROBE_AT_KEY: hint, "url": WEBHOOK_URL},
        secret_ref=hook_ref,
    )
    _record_transport.seed(WEBHOOK_URL, 200)
    async with tenant_session(admin) as s:
        await conn_service.submit_credentials(
            s, admin_id=admin, connection_id=hook, fields={"url": WEBHOOK_URL}, user_id=admin
        )
    row = await _row(admin, hook)
    assert row.status == "connected"
    assert NEXT_PROBE_AT_KEY not in (row.non_secret_config or {})


# --------------------------------------------------------------------------- #
# Item 12 — a disconnect releases the routing claim and forgets CRM state
# --------------------------------------------------------------------------- #

NUMBER = "+14155550123"
OTHER_NUMBER = "+14155550124"


async def _seed_serving_number(admin: uuid.UUID, iid: uuid.UUID, cfg_extra: dict | None = None):
    from app.connections.secrets import get_secret_store

    ref = f"vm/{admin}/{iid}/sms_sender"
    await get_secret_store().put(ref, {"authToken": "tok"})
    return await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="connected",
        cfg={"destination": NUMBER, "accountSid": ACCOUNT_SID, **(cfg_extra or {})},
        secret_ref=ref,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_disconnected_number_releases_its_claim_and_reconnect_re_proves_ownership():
    """Owner ruling 2026-09-14: disconnecting a channel connection RELEASES its
    number routing claim. Before, ``destination`` survived the disconnect, so a
    churned account held the number forever — its real owner could never connect
    it elsewhere, and inbound texts to it still resolved to the dead tenant. The
    number is kept under ``released_destination`` for the owner's convenience only;
    ownership is re-proven through the normal attach path, which re-runs the
    cross-tenant claim check."""
    from app.channels import routing
    from app.channels.phone_lifecycle import NumberAlreadyClaimed, attach_sms_number
    from app.connections import service
    from app.connections.service import RELEASED_DESTINATION_KEY

    owner = uuid.uuid4()
    owner_iid = await _seed_tenant(owner)
    cid = await _seed_serving_number(owner, owner_iid)
    async with tenant_session(owner) as s:
        assert await routing.owner_of_destination(s, "sms_sender", NUMBER) == owner
        out = await service.disconnect(s, admin_id=owner, connection_id=cid, user_id=None)
    assert out is not None and out["secret_deleted"] is True

    row = await _row(owner, cid)
    cfg = row.non_secret_config or {}
    assert "destination" not in cfg
    assert cfg[RELEASED_DESTINATION_KEY] == NUMBER
    assert cfg["accountSid"] == ACCOUNT_SID, "the owner's own account id is not a claim"
    async with tenant_session(owner) as s:
        assert await routing.owner_of_destination(s, "sms_sender", NUMBER) is None
    disconnects = [c for c in await _status_changes(owner) if c.get("reason") == "disconnect"]
    assert len(disconnects) == 1 and disconnects[0]["routing_claim_released"] is True

    # The number's next owner can now claim it through the normal attach path.
    newcomer = uuid.uuid4()
    newcomer_iid = await _seed_tenant(newcomer)
    async with tenant_session(newcomer) as s:
        claimed = await attach_sms_number(
            s,
            admin_id=newcomer,
            instance_id=newcomer_iid,
            e164_number=NUMBER,
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        assert claimed.status == "pending_carrier_registration"
        assert (claimed.non_secret_config or {})["destination"] == NUMBER

    # The churned account cannot silently take it back: the convenience copy is not
    # a claim, and re-attaching runs the same cross-tenant gate as any first attach.
    async with tenant_session(owner) as s:
        with pytest.raises(NumberAlreadyClaimed):
            await attach_sms_number(
                s,
                admin_id=owner,
                instance_id=owner_iid,
                e164_number=NUMBER,
                probe=_FixedProbe(OperabilityResult.OPERABLE),
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_owner_re_attaching_a_released_number_reclaims_it_and_a_new_number_re_derives():
    from app.channels.phone_lifecycle import attach_sms_number
    from app.connections import service
    from app.connections.service import RELEASED_DESTINATION_KEY

    owner = uuid.uuid4()
    iid = await _seed_tenant(owner)
    attested = {
        "carrier_registration_attested_at": NOW.isoformat(),
        "carrier_registration_attested_by": str(owner),
    }
    cid = await _seed_serving_number(owner, iid, attested)
    async with tenant_session(owner) as s:
        await service.disconnect(s, admin_id=owner, connection_id=cid, user_id=None)

    # Same number back: the claim is re-proven, the convenience copy is consumed and
    # the attestation of THAT number is kept.
    async with tenant_session(owner) as s:
        again = await attach_sms_number(
            s,
            admin_id=owner,
            instance_id=iid,
            e164_number=NUMBER,
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        cfg = dict(again.non_secret_config or {})
    assert cfg["destination"] == NUMBER
    assert RELEASED_DESTINATION_KEY not in cfg
    assert cfg["carrier_registration_attested_at"] == NOW.isoformat()

    # A DIFFERENT number after a disconnect re-derives from scratch: the attestation
    # was about the released number (number-change invalidation must still see it).
    async with tenant_session(owner) as s:
        await service.disconnect(s, admin_id=owner, connection_id=cid, user_id=None)
    async with tenant_session(owner) as s:
        changed = await attach_sms_number(
            s,
            admin_id=owner,
            instance_id=iid,
            e164_number=OTHER_NUMBER,
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        cfg = dict(changed.non_secret_config or {})
    assert cfg["destination"] == OTHER_NUMBER
    assert RELEASED_DESTINATION_KEY not in cfg
    assert "carrier_registration_attested_at" not in cfg


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_disconnected_mailbox_keeps_its_address_only_as_a_released_copy():
    from app.channels import routing
    from app.connections import service
    from app.connections.secrets import get_secret_store
    from app.connections.service import RELEASED_DESTINATION_KEY

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/email_sender"
    await get_secret_store().put(ref, {"access_token": "tok", "refresh_token": "r"})
    mailbox = "owner@acme.example"
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="email_sender",
        provider="outlook",
        auth_class="oauth_token",
        status="connected",
        cfg={"destination": mailbox, "address": mailbox, "mode": "byo_mailbox"},
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        await service.disconnect(s, admin_id=admin, connection_id=cid, user_id=None)
    cfg = (await _row(admin, cid)).non_secret_config or {}
    assert "destination" not in cfg and "address" not in cfg
    assert cfg[RELEASED_DESTINATION_KEY] == mailbox
    async with tenant_session(admin) as s:
        assert await routing.owner_of_destination(s, "email_sender", mailbox) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_disconnected_crm_forgets_where_the_leads_were_pushed():
    """After a CRM disconnect the Leads page still read "in your CRM" for every
    synced lead — the record ids were forgotten (round 5A) but the per-lead push
    state was not. The state is reset with the links, audited by count only."""
    from app.connections import service
    from app.connections.secrets import get_secret_store
    from app.models.lead import Lead

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/crm"
    await get_secret_store().put(ref, {"access_token": "tok"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        secret_ref=ref,
    )
    synced, failed, untouched = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Lead(
                id=synced,
                admin_id=admin,
                instance_id=iid,
                name="S",
                contact_identifier="s@lead.example",
                crm_push_status="synced",
                crm_pushed_at=NOW,
            )
        )
        s.add(
            Lead(
                id=failed,
                admin_id=admin,
                instance_id=iid,
                name="F",
                contact_identifier="f@lead.example",
                crm_push_status="failed",
                crm_push_detail="crm_http_500",
                crm_failure_notified_at=NOW,
            )
        )
        s.add(
            Lead(
                id=untouched,
                admin_id=admin,
                instance_id=iid,
                name="U",
                contact_identifier="u@lead.example",
            )
        )

    async with tenant_session(admin) as s:
        await service.disconnect(s, admin_id=admin, connection_id=cid, user_id=None)

    async with tenant_session(admin) as s:
        leads = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalars().all()
        assert len(leads) == 3
        for lead in leads:
            assert lead.crm_push_status is None, lead.name
            assert lead.crm_push_detail is None, lead.name
            assert lead.crm_pushed_at is None, lead.name
        cleared = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "crm_links_cleared",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(cleared) == 1
    payload = cleared[0].event_payload or {}
    assert payload["reason"] == "disconnect"
    assert payload["leads_reset"] == 2
    assert "lead.example" not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_provisioned_mailbox_address_is_lowercased():
    """Every other producer of a routable address case-folds it; the platform
    email provision was the one that stored the owner's capitals, so inbound mail
    to the lowercased form resolved no tenant."""
    from app.connections import service

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="  Sales@Example.COM ",
            instance_name="Acme",
            user_id=None,
        )
        cfg = dict(conn.non_secret_config or {})
    assert cfg["address"] == "sales@example.com"
    assert cfg["destination"] == "sales@example.com"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_disconnected_parked_number_reconnects_to_add_your_number_not_a_dead_end():
    """A number that never confirmed operable sits under ``pending_destination``.
    Kept there through a disconnect, the credential reconnect found "a number
    pending", wrote no status at all, and the row stayed ``not_connected`` with new
    credentials on file. It is folded into the released copy like a confirmed
    number, so the reconnect lands on the honest "add your number" state — and a
    second disconnect of the already-released row keeps the copy."""
    from app.connections import service
    from app.connections.secrets import get_secret_store
    from app.connections.service import RELEASED_DESTINATION_KEY, SMS_NEEDS_NUMBER_DETAIL

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
        status="not_operable_hosting_required",
        cfg={"pending_destination": NUMBER, "accountSid": ACCOUNT_SID},
        secret_ref=ref,
    )
    for _ in range(2):
        async with tenant_session(admin) as s:
            await service.disconnect(s, admin_id=admin, connection_id=cid, user_id=None)
        cfg = (await _row(admin, cid)).non_secret_config or {}
        assert "pending_destination" not in cfg and "destination" not in cfg
        assert cfg[RELEASED_DESTINATION_KEY] == NUMBER
    # A parked number was never a claim, so nothing was "released".
    disconnects = [c for c in await _status_changes(admin) if c.get("reason") == "disconnect"]
    assert [d["routing_claim_released"] for d in disconnects] == [False, False]
    assert all("released_destination" not in d for d in disconnects)

    async with tenant_session(admin) as s:
        conn = await service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=cid,
            fields={"accountSid": ACCOUNT_SID, "authToken": "new-token"},
            user_id=admin,
        )
        assert conn is not None
    row = await _row(admin, cid)
    assert row.status == "unconfigured"
    assert row.status_detail == SMS_NEEDS_NUMBER_DETAIL
    assert (row.non_secret_config or {})[RELEASED_DESTINATION_KEY] == NUMBER


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_disconnected_platform_address_is_released_and_cannot_re_verify_its_way_back():
    """A platform (ses) email row kept ``destination`` AND ``address`` through a
    disconnect "by design": the address stayed routable to the dead tenant, another
    account could never claim it, and Re-verify on the disconnected own-domain row
    flipped it straight back to connected without any claim check."""
    from app.channels import routing
    from app.channels.email_routing import MxDelegationResult
    from app.connections import service
    from app.connections.service import RELEASED_DESTINATION_KEY

    class _Delegated:
        async def check(self, domain: str, expected_mx: str) -> MxDelegationResult:
            return MxDelegationResult.DELEGATED

    address = "sales@acme.example"
    owner = uuid.uuid4()
    owner_iid = await _seed_tenant(owner)
    async with tenant_session(owner) as s:
        conn = await service.provision_email(
            s,
            admin_id=owner,
            instance_id=owner_iid,
            mode="own_domain",
            email_address=address,
            instance_name="Acme",
            user_id=None,
        )
        cid = conn.connection_id
        await service.reverify_email_routing(
            s, admin_id=owner, instance_id=owner_iid, probe=_Delegated()
        )
        assert conn.status == "connected"
        assert await routing.owner_of_destination(s, "email_sender", address) == owner

    async with tenant_session(owner) as s:
        await service.disconnect(s, admin_id=owner, connection_id=cid, user_id=None)
    cfg = (await _row(owner, cid)).non_secret_config or {}
    assert "destination" not in cfg and "address" not in cfg and "mode" not in cfg
    assert cfg[RELEASED_DESTINATION_KEY] == address
    disconnects = [c for c in await _status_changes(owner) if c.get("reason") == "disconnect"]
    assert disconnects[-1]["routing_claim_released"] is True
    assert disconnects[-1]["released_destination"] == address

    async with tenant_session(owner) as s:
        assert await service.resolve_email_address(s, owner, owner_iid) is None
        again = await service.reverify_email_routing(
            s, admin_id=owner, instance_id=owner_iid, probe=_Delegated()
        )
        assert again is not None and again.status == "not_connected"

    # The address's next owner can claim it; the churned row no longer blocks them.
    newcomer = uuid.uuid4()
    newcomer_iid = await _seed_tenant(newcomer)
    async with tenant_session(newcomer) as s:
        claimed = await service.provision_email(
            s,
            admin_id=newcomer,
            instance_id=newcomer_iid,
            mode="own_domain",
            email_address=address,
            instance_name="Newco",
            user_id=None,
        )
        assert (claimed.non_secret_config or {})["destination"] == address
    async with tenant_session(owner) as s:
        with pytest.raises(service.EmailAddressTaken):
            await service.provision_email(
                s,
                admin_id=owner,
                instance_id=owner_iid,
                mode="own_domain",
                email_address=address,
                instance_name="Acme",
                user_id=None,
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mailbox_claimed_under_other_casing_is_the_same_claim():
    from app.connections import service

    first, second = uuid.uuid4(), uuid.uuid4()
    first_iid = await _seed_tenant(first)
    second_iid = await _seed_tenant(second)
    async with tenant_session(first) as s:
        await service.provision_email(
            s,
            admin_id=first,
            instance_id=first_iid,
            mode="own_domain",
            email_address="sales@casing.example",
            instance_name="Acme",
            user_id=None,
        )
    async with tenant_session(second) as s:
        with pytest.raises(service.EmailAddressTaken):
            await service.provision_email(
                s,
                admin_id=second,
                instance_id=second_iid,
                mode="own_domain",
                email_address="Sales@Casing.Example",
                instance_name="Other",
                user_id=None,
            )


# --------------------------------------------------------------------------- #
# Item 2 follow-up — own keys submitted over an OAuth row drop the OAuth stamps
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_own_keys_over_an_oauth_twilio_row_stay_api_key_through_attach_and_sweep():
    """The live condition item 2's derivation opened: a tenant who connected Twilio
    one-click and later pasted their own API keys kept ``mode: oauth`` and the old
    ``token_expires_at`` on the row. The next number attach read the stale mode,
    re-stamped ``oauth_token`` over API-key material, and the sweep then tried to
    refresh a token that does not exist — a working credential went ``expired``
    with an attention email. The form submit now drops the OAuth stamps."""
    from datetime import timedelta

    from app.channels.phone_lifecycle import attach_sms_number
    from app.connections import service as conn_service
    from app.connections.health import ProbeResult
    from app.connections.secrets import get_secret_store

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    ref = f"vm/{admin}/{iid}/sms_sender"
    await get_secret_store().put(ref, {"access_token": "tw-bearer", "refresh_token": "r"})
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="oauth_token",
        status="unconfigured",
        cfg={
            "accountSid": ACCOUNT_SID,
            "accountName": "Acme",
            "mode": "oauth",
            "connectedAs": f"Acme ({ACCOUNT_SID})",
            "token_expires_at": (NOW - timedelta(hours=1)).isoformat(),
        },
        secret_ref=ref,
    )
    async with tenant_session(admin) as s:
        await conn_service.submit_credentials(
            s,
            admin_id=admin,
            connection_id=cid,
            fields={"accountSid": ACCOUNT_SID, "apiKeySid": "SK" + "1" * 32, "apiKeySecret": "s3"},
            user_id=admin,
        )
    row = await _row(admin, cid)
    assert row.auth_class == "api_key"
    cfg = row.non_secret_config or {}
    assert cfg["accountSid"] == ACCOUNT_SID
    for stamp in ("mode", "token_expires_at", "accountName", "connectedAs"):
        assert stamp not in cfg, stamp
    assert await get_secret_store().get(ref) == {"apiKeySid": "SK" + "1" * 32, "apiKeySecret": "s3"}

    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUMBER,
            probe=_FixedProbe(OperabilityResult.OPERABLE),
        )
        assert conn.auth_class == "api_key", "the attach must not resurrect the OAuth class"
        assert conn.status == "pending_carrier_registration"

    # The sweep probes it as the API-key row it is — no refresh, no false expiry.
    healthy = _AnswersProbe(ProbeResult(ok=True))
    assert await _sweep_with(admin, healthy, NOW) == {str(cid): "pending_carrier_registration"}
    assert healthy.calls == 1
    assert await _attention_emails(admin) == 0
