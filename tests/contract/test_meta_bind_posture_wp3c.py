"""Meta destination binds: proven-or-parked, and the claim oracle closed
(round 7 WP-3c items 11 + 12).

Item 11 — the bind used to be fail-OPEN outside live: ``meta_assets.lister_for_posture``
returns None in mock posture (no Meta to ask), the F082 ownership check was skipped,
and the id was written as the ROUTABLE destination anyway — a claim the platform
could not vouch for, that inbound would route on, and that another tenant would have
to fight. Now it mirrors ``phone_lifecycle.attach_sms_number``: an id whose ownership
Meta did not confirm is parked under ``pending_destination(s)`` with an honest
``status_detail`` and NO routing; only a bind a lister proves becomes the claim.

Item 12 — the Meta 409 was neither audited nor rate-limited, so it was the same
enumeration oracle the SMS attach closed under c21. It now refuses through the shared
helper: ``destination_claim_rejected`` under the CLAIMANT and the ``destination_claim``
bucket.

Helpers are the lifecycle suite's (same Meta OAuth fake), imported by name.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.admin.connections import META_UNPROVEN_DETAIL
from app.connections import meta_assets
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from sqlalchemy import select

from tests.contract.test_connection_lifecycle import (
    _admin_id,
    _connected_meta,
    _ready_account_with_luciel,
    _resolves_to,
)

pytestmark = [pytest.mark.contract]


class _EveryId(set):
    """What a lister returns for a grant that manages every id asked about."""

    def __contains__(self, item: object) -> bool:
        return True


def _meta_proves_every_id(monkeypatch) -> None:
    async def _lister(*, material, connection_type, transport):
        return _EveryId()

    monkeypatch.setattr(meta_assets, "lister_for_posture", lambda: _lister)


async def _bind_audits(admin_id: uuid.UUID) -> list[dict]:
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id))
        ).scalars()
        return [
            dict(e.event_payload or {})
            for e in rows
            if (e.event_payload or {}).get("action") == "bind_destination"
        ]


async def _claim_rejections(admin_id: uuid.UUID) -> list[dict]:
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "destination_claim_rejected",
                )
            )
        ).scalars()
        return [dict(e.event_payload or {}) for e in rows]


# --------------------------------------------------------------------------- #
# item 11 — proven or parked
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_in_mock_posture_a_meta_bind_is_parked_not_routable(client, monkeypatch):
    """No Meta to ask (the real mock posture, not a fake): the id is parked, the row
    says so honestly, nothing routes on it, and the audit row records it unproven."""
    assert meta_assets.lister_for_posture() is None  # the live condition under test
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    path = f"/api/v1/admin/connections/{cid}/destination"

    pnid = f"pnid-{uuid.uuid4().hex[:10]}"
    r = await client.put(path, json={"destination": pnid, "channel": "whatsapp"})
    assert r.status_code == 200, r.text
    cfg = r.json()["nonSecretConfig"]
    assert cfg.get("pending_destinations") == {"whatsapp": pnid}
    assert not cfg.get("destinations")
    assert r.json()["statusDetail"] == META_UNPROVEN_DETAIL
    assert await _resolves_to(admin_id, pnid) is None  # NOT a routable claim

    # The legacy scalar form parks the same way.
    page = f"pgid-{uuid.uuid4().hex[:10]}"
    r2 = await client.put(path, json={"destination": page})
    assert r2.status_code == 200, r2.text
    cfg2 = r2.json()["nonSecretConfig"]
    assert cfg2.get("pending_destination") == page
    assert "destination" not in cfg2
    assert await _resolves_to(admin_id, page) is None

    audits = await _bind_audits(admin_id)
    assert len(audits) == 2
    assert {a["proven"] for a in audits} == {False}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_bind_a_lister_proves_promotes_the_parked_id_and_clears_the_notice(
    client, monkeypatch
):
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    path = f"/api/v1/admin/connections/{cid}/destination"
    pnid = f"pnid-{uuid.uuid4().hex[:10]}"

    parked = await client.put(path, json={"destination": pnid, "channel": "whatsapp"})
    assert parked.status_code == 200 and parked.json()["statusDetail"] == META_UNPROVEN_DETAIL

    _meta_proves_every_id(monkeypatch)
    proven = await client.put(path, json={"destination": pnid, "channel": "whatsapp"})
    assert proven.status_code == 200, proven.text
    cfg = proven.json()["nonSecretConfig"]
    assert cfg.get("destinations") == {"whatsapp": pnid}
    assert "pending_destinations" not in cfg
    assert proven.json()["statusDetail"] is None  # OUR notice cleared, nothing else touched
    assert await _resolves_to(admin_id, pnid) == admin_id

    audits = await _bind_audits(admin_id)
    assert [a["proven"] for a in audits] == [False, True]
    assert audits[1]["old_destination"] == pnid  # the parked id is what it replaced


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_parked_id_holds_no_claim_so_the_proven_owner_still_binds_it(client, monkeypatch):
    """The squatting mirror of test_claim_hardening: a tenant who parked an id it could
    not prove does not block the tenant whose grant Meta vouches for — and once the
    real owner holds it, the parker's later re-bind is the ordinary 409."""
    await _ready_account_with_luciel(client)
    parker = _admin_id(client)
    parker_cid = await _connected_meta(client, monkeypatch)
    pnid = f"pnid-{uuid.uuid4().hex[:10]}"
    parked = await client.put(
        f"/api/v1/admin/connections/{parker_cid}/destination",
        json={"destination": pnid, "channel": "whatsapp"},
    )
    assert parked.status_code == 200 and not parked.json()["nonSecretConfig"].get("destinations")

    await client.post("/api/v1/auth/logout")
    await _ready_account_with_luciel(client)
    owner = _admin_id(client)
    assert owner != parker
    owner_cid = await _connected_meta(client, monkeypatch)
    _meta_proves_every_id(monkeypatch)
    ok = await client.put(
        f"/api/v1/admin/connections/{owner_cid}/destination",
        json={"destination": pnid, "channel": "whatsapp"},
    )
    assert ok.status_code == 200, ok.text
    assert await _resolves_to(owner, pnid) == owner

    # The parker's row still only PARKS the id: no routable entry was ever written,
    # so the owner's claim is the only one the resolver can see.
    async with tenant_session(parker) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.connection_id == uuid.UUID(parker_cid)
                )
            )
        ).scalar_one()
        cfg = row.non_secret_config or {}
        assert cfg.get("pending_destinations") == {"whatsapp": pnid}
        assert not cfg.get("destinations")


# --------------------------------------------------------------------------- #
# item 12 — the Meta 409 is audited and rate-limited
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_repeated_meta_claims_of_a_held_id_are_audited_and_rate_limited(client, monkeypatch):
    """Same contract as the SMS attach (c21): every refusal writes
    ``destination_claim_rejected`` under the CLAIMANT (own transaction — the 409 rolls
    the request's back), never under the holder, and the burst closes with a 429."""
    _meta_proves_every_id(monkeypatch)
    await _ready_account_with_luciel(client)
    holder = _admin_id(client)
    holder_cid = await _connected_meta(client, monkeypatch)
    shared = f"pnid-{uuid.uuid4().hex[:10]}"
    assert (
        await client.put(
            f"/api/v1/admin/connections/{holder_cid}/destination",
            json={"destination": shared, "channel": "whatsapp"},
        )
    ).status_code == 200

    await client.post("/api/v1/auth/logout")
    await _ready_account_with_luciel(client)
    claimant = _admin_id(client)
    assert claimant != holder
    claimant_cid = await _connected_meta(client, monkeypatch)
    path = f"/api/v1/admin/connections/{claimant_cid}/destination"

    statuses = []
    for _ in range(5):
        r = await client.put(path, json={"destination": shared, "channel": "whatsapp"})
        statuses.append(r.status_code)
    assert statuses[0] == 409
    assert 429 in statuses  # the oracle closes after the burst
    assert 200 not in statuses

    rejections = await _claim_rejections(claimant)
    assert len(rejections) == 5  # every refusal, including the rate-limited ones
    assert {p["connection_type"] for p in rejections} == {"channel_auth"}
    assert {p["destination_suffix"] for p in rejections} == {shared[-4:]}
    assert all("number_suffix" not in p for p in rejections)  # the SMS reader's key only
    assert not any(shared in str(p) for p in rejections)  # never the full id
    assert await _claim_rejections(holder) == []  # never under the victim
    assert await _resolves_to(holder, shared) == holder  # the holder's claim is intact


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_meta_refusal_is_recorded_even_when_meta_cannot_be_asked(client, monkeypatch):
    """The ownership gate runs BEFORE the lister, so a held id is refused (and the
    refusal recorded) in mock posture too — a parked claim never leaks as a probe."""
    assert meta_assets.lister_for_posture() is None
    await _ready_account_with_luciel(client)
    holder = _admin_id(client)
    holder_iid_row = await _connected_meta(client, monkeypatch)
    shared = f"pnid-{uuid.uuid4().hex[:10]}"
    # The holder's claim is a PROVEN one (written while a lister vouched for it).
    _meta_proves_every_id(monkeypatch)
    assert (
        await client.put(
            f"/api/v1/admin/connections/{holder_iid_row}/destination",
            json={"destination": shared, "channel": "messenger"},
        )
    ).status_code == 200
    monkeypatch.setattr(meta_assets, "lister_for_posture", lambda: None)

    await client.post("/api/v1/auth/logout")
    await _ready_account_with_luciel(client)
    claimant = _admin_id(client)
    claimant_cid = await _connected_meta(client, monkeypatch)
    clash = await client.put(
        f"/api/v1/admin/connections/{claimant_cid}/destination",
        json={"destination": shared, "channel": "messenger"},
    )
    assert clash.status_code == 409, clash.text
    assert len(await _claim_rejections(claimant)) == 1
    assert await _claim_rejections(holder) == []  # never under the victim


# --------------------------------------------------------------------------- #
# phase-2 security review — the limiter closes the oracle it was added for
# --------------------------------------------------------------------------- #


async def _holder_with_a_proven_claim(client, monkeypatch) -> tuple[uuid.UUID, str]:
    """An account whose grant Meta vouched for holds ``shared`` as a routable claim."""
    _meta_proves_every_id(monkeypatch)
    await _ready_account_with_luciel(client)
    holder = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    shared = f"pnid-{uuid.uuid4().hex[:10]}"
    r = await client.put(
        f"/api/v1/admin/connections/{cid}/destination",
        json={"destination": shared, "channel": "whatsapp"},
    )
    assert r.status_code == 200, r.text
    assert await _resolves_to(holder, shared) == holder
    await client.post("/api/v1/auth/logout")
    return holder, shared


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_once_refusals_spend_the_bucket_every_claim_attempt_answers_alike(
    client, monkeypatch
):
    """The bucket used to be charged and read only on the held-id path, so 409-or-429
    meant "another account holds this" and a 200 meant "free" — before the burst and
    after it. It is now consulted BEFORE the claim is evaluated: once refusals have
    spent it, an id nobody holds gets the very same 429 (and is not parked), and so
    does an SMS attach — the bucket is shared by every claim point."""
    holder, shared = await _holder_with_a_proven_claim(client, monkeypatch)
    monkeypatch.setattr(meta_assets, "lister_for_posture", lambda: None)  # mock posture

    await _ready_account_with_luciel(client)
    claimant = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    path = f"/api/v1/admin/connections/{cid}/destination"
    for _ in range(3):  # the burst: the held id is refused, audited, charged
        r = await client.put(path, json={"destination": shared, "channel": "whatsapp"})
        assert r.status_code == 409, r.text

    held = await client.put(path, json={"destination": shared, "channel": "whatsapp"})
    free_id = f"pnid-{uuid.uuid4().hex[:10]}"
    free = await client.put(path, json={"destination": free_id, "channel": "whatsapp"})
    assert held.status_code == free.status_code == 429
    assert held.json() == free.json()  # nothing in the answer says which was held
    attach = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": "+16045550177"},
    )
    assert attach.status_code == 429
    assert attach.json() == free.json()

    listed = (await client.get("/api/v1/admin/connections")).json()
    (row,) = [c for c in listed if c["connectionId"] == cid]
    assert free_id not in str(row["nonSecretConfig"])  # the free id was not parked either
    assert len(await _claim_rejections(claimant)) == 6  # every refused attempt, recorded
    assert not any(shared in str(p) or free_id in str(p) for p in await _claim_rejections(claimant))
    assert await _claim_rejections(holder) == []
    assert await _resolves_to(holder, shared) == holder


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_grant_that_does_not_cover_the_id_gets_one_answer_held_or_not(client, monkeypatch):
    """Where Meta can be asked, the grant must cover the id BEFORE the cross-tenant
    check: a claimant whose grant manages neither id gets the same 422 for the one
    another account holds as for one nobody does — and no claim refusal is recorded,
    because no claim was ever evaluated."""
    holder, shared = await _holder_with_a_proven_claim(client, monkeypatch)

    async def _lists_nothing(*, material, connection_type, transport):
        return set()

    monkeypatch.setattr(meta_assets, "lister_for_posture", lambda: _lists_nothing)
    await _ready_account_with_luciel(client)
    claimant = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    path = f"/api/v1/admin/connections/{cid}/destination"

    held = await client.put(path, json={"destination": shared, "channel": "whatsapp"})
    free = await client.put(
        path, json={"destination": f"pnid-{uuid.uuid4().hex[:10]}", "channel": "whatsapp"}
    )
    assert held.status_code == free.status_code == 422
    assert held.json() == free.json()
    assert "doesn't list that id" in held.json()["message"]
    assert await _claim_rejections(claimant) == []
    assert await _resolves_to(holder, shared) == holder


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connection_with_no_meta_grant_learns_nothing_about_held_ids(client, monkeypatch):
    """The cheapest prober: a channel_auth row started but never authorized. It used
    to get 409 for a held id and 422 for a free one; both now answer "holds no
    authorization yet"."""
    from tests.contract.test_connection_lifecycle import _configure_meta

    holder, shared = await _holder_with_a_proven_claim(client, monkeypatch)
    _meta_proves_every_id(monkeypatch)  # Meta could be asked — the grant is what is missing
    await _ready_account_with_luciel(client)
    claimant = _admin_id(client)
    _configure_meta(monkeypatch)
    started = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "channel_auth", "provider": "meta"}
    )
    assert started.status_code == 200, started.text
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "channel_auth")
    path = f"/api/v1/admin/connections/{cid}/destination"

    held = await client.put(path, json={"destination": shared, "channel": "whatsapp"})
    free = await client.put(
        path, json={"destination": f"pnid-{uuid.uuid4().hex[:10]}", "channel": "whatsapp"}
    )
    assert held.status_code == free.status_code == 422
    assert held.json() == free.json()
    assert "holds no authorization yet" in held.json()["message"]
    assert await _claim_rejections(claimant) == []
    assert await _resolves_to(holder, shared) == holder
