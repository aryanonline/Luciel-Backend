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
