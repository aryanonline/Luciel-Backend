"""Widget embed keys — minted unique, by construction AND by the database.

``instances.embed_key_public_id`` is the ONE thing the widget data plane resolves a
tenant by (``channel_resolve_embed_key``), so two instances holding the same key
would be a routing ambiguity, not a tidiness problem. Migration 0067 (round 7 WP-3c
item 14) made ``ix_instances_embed_key`` unique; this module is the one place a key
is minted, and it retries on the (astronomically unlikely, but now structurally
refused) conflict instead of surfacing a 500 to the owner.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import Instance

EMBED_KEY_PREFIX = "vm_live_"
EMBED_KEY_INDEX = "ix_instances_embed_key"
MINT_ATTEMPTS = 5


def mint_embed_key() -> str:
    """A fresh public embed key (a public identifier, not a secret)."""
    return f"{EMBED_KEY_PREFIX}{uuid.uuid4().hex[:12]}"


def _is_embed_key_conflict(exc: IntegrityError) -> bool:
    return EMBED_KEY_INDEX in str(exc.orig or exc)


async def assign_fresh_embed_key(
    session: AsyncSession, inst: Instance, *, attempts: int = MINT_ATTEMPTS
) -> str:
    """Give ``inst`` a freshly minted key and FLUSH it under a savepoint.

    RLS hides other tenants' rows, so no SELECT can prove a key is free; only the
    unique index can. Each attempt writes the key inside a SAVEPOINT: a conflict
    rolls back just that write, the row stays in the session, and the next attempt
    mints again. Any other integrity error, or a conflict on the last attempt,
    propagates unchanged — never a silent fallback to a non-unique key.

    The key is assigned INSIDE the savepoint on purpose: ``begin_nested`` flushes
    whatever is already dirty BEFORE it emits SAVEPOINT, so a key assigned outside
    would be written (and would collide) in the caller's own transaction, killing
    it. A not-yet-flushed row is therefore inserted first with its current key and
    the fresh key lands as an UPDATE under the savepoint."""
    if inst not in session:
        session.add(inst)
    for attempt in range(1, attempts + 1):
        try:
            async with session.begin_nested():
                inst.embed_key_public_id = mint_embed_key()
                await session.flush()
        except IntegrityError as exc:
            if not _is_embed_key_conflict(exc) or attempt == attempts:
                raise
            # The savepoint rollback expired the row; reload it so neither the next
            # attempt nor the caller trips a lazy load on an async session.
            await session.refresh(inst)
            continue
        return str(inst.embed_key_public_id)
    raise RuntimeError("unreachable: assign_fresh_embed_key exhausted its attempts")
