"""One embed key, one instance (round 7 WP-3c item 14).

``instances.embed_key_public_id`` is the ONE thing the widget data plane resolves a
tenant by (``channel_resolve_embed_key``), yet it carried a plain index: two
instances holding one key was a routing ambiguity the database would happily
store. Migration 0067 made ``ix_instances_embed_key`` unique, and every mint site
(create / rotate / restore-without-stash) goes through
``lifecycle.embed_keys.assign_fresh_embed_key``, which re-mints on a conflict
instead of surfacing a 500 — and never falls back to a non-unique key.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import embed_keys, state_machine
from app.lifecycle.embed_keys import EMBED_KEY_PREFIX, MINT_ATTEMPTS, assign_fresh_embed_key
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


async def _account(admin: uuid.UUID) -> None:
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
        await s.flush()


def _instance(admin: uuid.UUID, key: str | None) -> Instance:
    return Instance(
        id=uuid.uuid4(),
        admin_id=admin,
        name="Key Co",
        state=LucielState.active,
        embed_key_public_id=key,
        channels=[],
        tools=[],
        escalation={},
        personality={},
    )


async def _seed(admin: uuid.UUID, key: str) -> None:
    await _account(admin)
    async with tenant_session(admin) as s:
        s.add(_instance(admin, key))
        await s.flush()


async def _key_of(admin: uuid.UUID) -> str | None:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(Instance.embed_key_public_id).where(Instance.admin_id == admin))
        ).scalar_one()


def _mint_sequence(monkeypatch, keys: list[str]) -> list[str]:
    """Make the mint return ``keys`` in order (the last one forever); returns the log."""
    minted: list[str] = []
    remaining = list(keys)

    def _mint() -> str:
        key = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        minted.append(key)
        return key

    monkeypatch.setattr(embed_keys, "mint_embed_key", _mint)
    return minted


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_unique_index_refuses_a_second_instance_on_one_key_structurally():
    a, b = uuid.uuid4(), uuid.uuid4()
    key = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    await _seed(a, key)
    await _account(b)
    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            s.add(_instance(b, key))
            await s.flush()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_colliding_mint_is_retried_and_the_row_keeps_a_fresh_unique_key(monkeypatch):
    """RLS hides the other tenant's row, so only the index can say a key is taken:
    the first mint collides, the savepoint rolls back just that flush, and the
    second mint lands — the caller's transaction is intact throughout."""
    holder, newcomer = uuid.uuid4(), uuid.uuid4()
    taken = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    fresh = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    await _seed(holder, taken)
    await _account(newcomer)
    minted = _mint_sequence(monkeypatch, [taken, fresh])

    async with tenant_session(newcomer) as s:
        inst = _instance(newcomer, None)
        s.add(inst)
        assert await assign_fresh_embed_key(s, inst) == fresh
        # Still one usable transaction: more work after the retry commits with it.
        inst.name = "Key Co (renamed)"
    assert minted == [taken, fresh]
    assert await _key_of(newcomer) == fresh
    assert await _key_of(holder) == taken
    async with tenant_session(newcomer) as s:
        assert (
            await s.execute(select(Instance.name).where(Instance.admin_id == newcomer))
        ).scalar_one() == "Key Co (renamed)"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mint_that_keeps_colliding_raises_instead_of_writing_a_duplicate(monkeypatch):
    holder, newcomer = uuid.uuid4(), uuid.uuid4()
    taken = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    await _seed(holder, taken)
    await _account(newcomer)
    minted = _mint_sequence(monkeypatch, [taken])

    with pytest.raises(IntegrityError):
        async with tenant_session(newcomer) as s:
            inst = _instance(newcomer, None)
            s.add(inst)
            await assign_fresh_embed_key(s, inst)
    assert len(minted) == MINT_ATTEMPTS  # bounded, and never a silent non-unique fallback
    async with tenant_session(newcomer) as s:
        assert (
            await s.execute(select(Instance.id).where(Instance.admin_id == newcomer))
        ).scalar_one_or_none() is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_of_a_legacy_row_without_a_stash_mints_through_the_retrying_path(
    monkeypatch,
):
    """A row that entered grace before the stash column existed mints fresh on
    restore — through the same unique, retried mint as create and rotate."""
    holder, legacy = uuid.uuid4(), uuid.uuid4()
    taken = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    fresh = f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"
    await _seed(holder, taken)
    await _seed(legacy, f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}")
    async with tenant_session(legacy) as s:
        await state_machine.delete_luciel(s, legacy, actor_user_id=legacy)
        inst = (await s.execute(select(Instance).where(Instance.admin_id == legacy))).scalar_one()
        inst.embed_key_stashed = None  # the pre-stash legacy shape
    _mint_sequence(monkeypatch, [taken, fresh])

    async with tenant_session(legacy) as s:
        restored = await state_machine.restore(s, legacy, actor_user_id=legacy)
        assert restored.state == LucielState.active
    assert await _key_of(legacy) == fresh
    assert await _key_of(holder) == taken
