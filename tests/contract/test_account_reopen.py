"""Reopen and re-export on the closed-account surface (round 7 WP-5 / owner decision D5).

Owner decision D5 (2026-09-14): after account closure the owner can sign in for 30
days to a reduced surface that offers exactly two actions — REOPEN the account
(restores it to the state before closure, Luciel included if it was deleted by the
closure) and RE-REQUEST the export; nothing else is reachable.

Live condition before this: closure was "recoverable for 30 days" in the product's
own copy and terminal in the code — ``AccountState.closed`` had no way back, and the
one export link the closure emailed could not be asked for again.

The three contract cases the decision names are here by name: a closed account can
reopen on day 2, cannot on day 31, and cannot reach ``/admin/luciel`` while closed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.lifecycle.export_store import LocalExportStore
from app.models.audit import AdminAuditLog
from app.models.connection import QUIESCING, InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select, update

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

PASSWORD = "pw-123456"  # noqa: S105 — test fixture credential


async def _signup_verify_login(client) -> tuple[uuid.UUID, str]:
    from app.auth import access

    email = f"reopen-{uuid.uuid4().hex[:8]}@x.com"
    acct, token = await access.signup(email, PASSWORD)
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return acct.id, email


async def _create_luciel(client) -> dict[str, object]:
    r = await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _close_and_sign_back_in(client, email: str) -> None:
    r = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True})
    assert r.status_code == 204, r.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["account"]["state"] == "closed"


async def _age_closure(admin_id: uuid.UUID, *, days: int) -> None:
    """Move the closure ``days`` into the past: the account's clock and — when the
    closure deleted the Luciel — the grace window that started at the same instant."""
    then = datetime.now(UTC) - timedelta(days=days)
    async with tenant_session(admin_id) as s:
        await s.execute(update(Account).where(Account.id == admin_id).values(closed_at=then))
        await s.execute(
            update(Instance)
            .where(
                Instance.admin_id == admin_id,
                Instance.state == LucielState.luciel_grace_window,
            )
            .values(grace_window_started_at=then, soft_deleted_at=then)
        )


async def _events(admin_id: uuid.UUID, event_type: str) -> list[dict[str, object]]:
    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == event_type,
                    )
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_closed_account_reopens_on_day_two_exactly_as_it_was(client):
    admin_id, email = await _signup_verify_login(client)
    luciel = await _create_luciel(client)
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=uuid.UUID(str(luciel["instanceId"])),
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
                non_secret_config={},
            )
        )
    await _close_and_sign_back_in(client, email)
    await _age_closure(admin_id, days=2)

    # While closed: the Luciel is deleted-with-grace and its surface is NOT reachable —
    # not even its own Restore. The typed refusal is what the frontend renders from.
    async with tenant_session(admin_id) as s:
        conn = (await s.execute(select(InstanceConnection))).scalar_one()
    assert conn.status == QUIESCING
    for refused in (
        await client.get("/api/v1/admin/luciel"),
        await client.post("/api/v1/admin/luciel/restore"),
    ):
        assert refused.status_code == 403
        assert refused.json()["code"] == "account_closed"

    r = await client.post("/api/v1/admin/account/reopen")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "state": "verified",
        "lucielRestored": True,
        "lucielState": "active",
        "billingState": "free_cap",
    }

    # The SAME session keeps working — no second login — and the whole surface is back.
    back = await client.get("/api/v1/admin/luciel")
    assert back.status_code == 200
    assert back.json()["state"] == "active"
    assert back.json()["embedKeyPublicId"] == luciel["embedKeyPublicId"], (
        "the snippet already on the customer's site must work again without an edit"
    )
    status = (await client.get("/api/v1/admin/account")).json()
    assert status == {
        "state": "verified",
        "closedAt": None,
        "reopenUntil": None,
        "canReopen": False,
        "lucielRestoredOnReopen": False,
    }
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
        conn = (await s.execute(select(InstanceConnection))).scalar_one()
    assert (acct.state, acct.closed_at) == (AccountState.verified, None)
    assert conn.status == "connected", "connections come back at their pre-closure status"

    (reopened,) = await _events(admin_id, "account_reopened")
    assert reopened["luciel_restored"] is True
    assert reopened["luciel_state"] == "active"
    assert reopened["billing_state"] == "free_cap"
    assert reopened["subscription"] == "none"
    transitions = await _events(admin_id, "lifecycle_transition")
    assert {"from": "account_closing", "to": "account_open", "trigger": "account_reopen"} in (
        transitions
    )
    # The owner-facing audit log says what came back.
    log = (await client.get("/api/v1/admin/usage/audit-log")).json()
    (row,) = [e for e in log if e["eventType"] == "account_reopened"]
    assert row["detail"] == "Luciel restored (active)"

    # Reopened means open: a second reopen has nothing to act on.
    again = await client.post("/api/v1/admin/account/reopen")
    assert again.status_code == 409


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_closed_account_cannot_reopen_or_re_export_on_day_thirty_one(client):
    admin_id, email = await _signup_verify_login(client)
    await _create_luciel(client)
    await _close_and_sign_back_in(client, email)
    await _age_closure(admin_id, days=31)

    for refused in (
        await client.post("/api/v1/admin/account/reopen"),
        await client.post("/api/v1/admin/account/export"),
        await client.get("/api/v1/admin/account"),
        await client.get("/api/v1/admin/luciel"),
    ):
        assert refused.status_code == 403, (refused.request.url.path, refused.text)
        body = refused.json()
        assert body["code"] == "account_closed"
        assert datetime.fromisoformat(body["detail"]["reopenUntil"]) < datetime.now(UTC)

    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
        inst = (await s.execute(select(Instance))).scalar_one()
    assert acct.state == AccountState.closed, "a refused reopen changes nothing"
    assert inst.state == LucielState.luciel_grace_window
    assert await _events(admin_id, "account_reopened") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_that_was_paused_comes_back_paused(client):
    """ "The state before closure": restore alone returns ACTIVE (§3.6.4). A Luciel the
    owner had paused must not start answering customers because the account reopened."""
    admin_id, email = await _signup_verify_login(client)
    await _create_luciel(client)
    assert (await client.post("/api/v1/admin/luciel/pause")).status_code == 200
    await _close_and_sign_back_in(client, email)

    r = await client.post("/api/v1/admin/account/reopen")
    assert r.status_code == 200, r.text
    assert (r.json()["lucielRestored"], r.json()["lucielState"]) == (True, "paused")
    assert (await client.get("/api/v1/admin/luciel")).json()["state"] == "paused"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_the_owner_deleted_before_closing_is_not_resurrected(client):
    """The reopen restores what the CLOSURE took. A Luciel the owner deleted on purpose
    days earlier stays deleted — and its own Restore, inside its own grace window, is
    reachable again the moment the account is open."""
    admin_id, email = await _signup_verify_login(client)
    await _create_luciel(client)
    assert (await client.post("/api/v1/admin/luciel/delete")).status_code == 200
    r = await client.post("/api/v1/admin/account/close", json={})
    assert r.status_code == 204, r.text
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert (await client.get("/api/v1/admin/account")).json()["lucielRestoredOnReopen"] is False

    r = await client.post("/api/v1/admin/account/reopen")
    assert r.status_code == 200, r.text
    assert (r.json()["lucielRestored"], r.json()["lucielState"]) == (False, "luciel_grace_window")
    (reopened,) = await _events(admin_id, "account_reopened")
    assert reopened["luciel_restored"] is False

    restored = await client.post("/api/v1/admin/luciel/restore")
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_closed_account_can_re_request_its_export(client):
    """The closure emailed ONE 7-day link. The reduced surface can ask again: same
    bundle, same 7-day link, same email — and the door serves it."""
    LocalExportStore.reset()
    admin_id, email = await _signup_verify_login(client)
    await _create_luciel(client)
    await _close_and_sign_back_in(client, email)
    assert len(LocalExportStore.keys()) == 1, "the closure's own export-first bundle"

    r = await client.post("/api/v1/admin/account/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl_days"] == 7
    assert body["emailOutcome"] == "recorded"
    assert len(LocalExportStore.keys()) == 2
    door = body["download_url"].split("/api/v1", 1)[1]
    opened = await client.get(f"/api/v1{door}", follow_redirects=False)
    assert opened.status_code == 302
    assert f"vm-export-bundles/{admin_id}/" in opened.headers["location"]

    ready = await _events(admin_id, "data_export_ready")
    assert [p.get("context") for p in ready] == ["account_close", "account_closed_reexport"]
    # Re-requesting is not reopening.
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
    assert acct.state == AccountState.closed
    LocalExportStore.reset()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reopening_an_account_that_is_not_closed_is_a_conflict(client):
    admin_id, _ = await _signup_verify_login(client)
    r = await client.post("/api/v1/admin/account/reopen")
    assert r.status_code == 409
    assert await _events(admin_id, "account_reopened") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_signing_up_on_a_closed_accounts_email_does_not_take_it_over(client):
    """A closed account's email is not released until the day-30 purge. Signup used to
    treat the row as a pending signup: it overwrote the password and flipped the state
    to ``unverified`` — locking the owner out of reopen / re-export with their own
    password, and taking the row out of the purge's reach."""
    admin_id, email = await _signup_verify_login(client)
    await _close_and_sign_back_in(client, email)

    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "someone-elses-pw", "captchaToken": "t"},
    )
    assert r.status_code == 200
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
    assert acct.state == AccountState.closed
    assert acct.pending_verify_jti is None, "no verification link was minted for it"

    stolen = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "someone-elses-pw"}
    )
    assert stolen.status_code == 401
    owner = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert owner.status_code == 200
    assert (await client.post("/api/v1/admin/account/reopen")).status_code == 200
