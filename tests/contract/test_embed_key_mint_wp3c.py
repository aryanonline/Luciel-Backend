"""The owner-facing mint sites re-try a colliding embed key (round 7 WP-3c item 14).

Migration 0067 made the key unique; a collision on first-run create or on rotate
must NOT surface as a 500 to the owner — the mint goes through
``lifecycle.embed_keys.assign_fresh_embed_key`` and lands on a fresh key.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import embed_keys
from app.lifecycle.embed_keys import EMBED_KEY_PREFIX
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

from tests.contract.test_connection_lifecycle import _admin_id, _ready_account_with_luciel

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _holder_of(key: str) -> uuid.UUID:
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=uuid.uuid4(),
                admin_id=admin,
                name="Holder Co",
                state=LucielState.active,
                embed_key_public_id=key,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return admin


def _collide_once(monkeypatch, taken: str, fresh: str) -> list[str]:
    minted: list[str] = []

    def _mint() -> str:
        key = taken if not minted else fresh
        minted.append(key)
        return key

    monkeypatch.setattr(embed_keys, "mint_embed_key", _mint)
    return minted


async def _key_of(admin: uuid.UUID) -> str | None:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(Instance.embed_key_public_id).where(Instance.admin_id == admin))
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_first_run_create_survives_a_colliding_mint(client, monkeypatch):
    taken = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    fresh = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    holder = await _holder_of(taken)
    minted = _collide_once(monkeypatch, taken, fresh)

    await _ready_account_with_luciel(client)  # POST /admin/luciel is inside
    admin_id = _admin_id(client)
    assert minted == [taken, fresh]
    assert await _key_of(admin_id) == fresh
    assert await _key_of(holder) == taken
    me = await client.get("/api/v1/admin/luciel")
    assert me.status_code == 200, me.text
    assert me.json()["embedKeyPublicId"] == fresh


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_rotate_survives_a_colliding_mint(client, monkeypatch):
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    before = await _key_of(admin_id)
    taken = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    fresh = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    holder = await _holder_of(taken)
    minted = _collide_once(monkeypatch, taken, fresh)

    r = await client.post("/api/v1/admin/luciel/rotate-embed-key")
    assert r.status_code == 200, r.text
    assert r.json()["embedKeyPublicId"] == fresh
    assert minted == [taken, fresh]
    assert await _key_of(admin_id) == fresh != before
    assert await _key_of(holder) == taken
