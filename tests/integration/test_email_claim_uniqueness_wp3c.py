"""Email claims are case-insensitive (round 7 WP-3c item 10, owner ruling 2026-09-14).

Before this: ``verify.py`` lower-cased the Graph mailbox, the SES parser passed the
recipient through as delivered, and the 0044 resolver compared byte-exactly — so
``Owner@Fabrikam.Example`` and ``owner@fabrikam.example`` were two claims on one
mailbox (a second tenant slipped past the ownership gate) and an inbound addressed
with capitals resolved to nobody.

Pinned here in the c21 shape (gate, lock, structural belt), the email mirror of
``tests/integration/test_claim_hardening.py``:

  * the gate (``routing.owner_of_destination``) and the resolver case-fold an
    email claim;
  * the 0067 partial unique index on ``lower(destination)`` refuses a case variant
    of a live claim even from a code path that forgot the gate — and only for
    non-revoked rows, exactly like 0045;
  * two tenants racing one mailbox (spelled differently) admit exactly one.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from app.channels import routing
from app.channels.routing import normalize_email_destination
from app.connections import service
from app.connections.service import EmailAddressTaken
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

MAILBOX = "owner@fabrikam.example"
MAILBOX_CAPS = "Owner@Fabrikam.Example"


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
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
                id=iid,
                admin_id=admin,
                name="Mail Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _mailbox_row(
    admin: uuid.UUID, iid: uuid.UUID, destination: str, *, status: str = "connected"
) -> None:
    """A BYO-mailbox row written the way every producer writes it after item 10
    (``verify.py`` lower-cases the Graph address) — or, for the structural tests,
    deliberately NOT, to prove the database refuses the variant regardless."""
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status=status,
                non_secret_config={
                    "address": destination,
                    "mode": "byo_mailbox",
                    "destination": destination,
                },
            )
        )
        await s.flush()


async def _email_row(admin: uuid.UUID) -> InstanceConnection | None:
    async with tenant_session(admin) as s:
        return (
            (
                await s.execute(
                    select(InstanceConnection).where(
                        InstanceConnection.admin_id == admin,
                        InstanceConnection.connection_type == "email_sender",
                    )
                )
            )
            .scalars()
            .first()
        )


def test_the_canonical_email_spelling_is_lower_case_and_trimmed() -> None:
    assert normalize_email_destination("  Owner@Fabrikam.Example ") == MAILBOX
    assert normalize_email_destination(MAILBOX) == MAILBOX


# --------------------------------------------------------------------------- #
# the gate and the resolver
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_ownership_gate_and_the_resolver_case_fold_an_email_claim():
    """The holder's row is stored canonical (lower case). A claim — or an inbound —
    spelled with capitals is the SAME mailbox: the gate names the holder and the
    resolver routes to them, instead of "unclaimed" / "unknown destination"."""
    holder, claimant = uuid.uuid4(), uuid.uuid4()
    holder_iid = await _seed(holder)
    await _seed(claimant)
    await _mailbox_row(holder, holder_iid, MAILBOX)

    async with tenant_session(claimant) as s:
        # RLS hides the holder's row from the claimant; the SECURITY DEFINER path sees it.
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX_CAPS) == holder
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX) == holder
        ctx = await routing.resolve_destination(s, "email_sender", MAILBOX_CAPS)
        assert (ctx.admin_id, ctx.instance_id) == (holder, holder_iid)


# --------------------------------------------------------------------------- #
# the structural belt (0067 partial unique index on lower(destination))
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_unique_index_refuses_a_case_variant_of_a_live_claim_structurally():
    """Belt for the gate: even a code path that FORGOT the gate — or one that still
    stores the typed spelling — cannot write a second live claim on one mailbox."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)
    await _mailbox_row(a, a_iid, MAILBOX)
    with pytest.raises(IntegrityError):
        await _mailbox_row(b, b_iid, MAILBOX_CAPS)
    assert await _email_row(b) is None  # nothing half-written


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_revoked_row_holds_no_claim_on_the_mailbox():
    """Scope mirrors 0045: non-revoked rows only. A disconnected mailbox does not
    block its real owner (or the same owner reconnecting on a fresh row)."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)
    await _mailbox_row(a, a_iid, MAILBOX, status="revoked")
    await _mailbox_row(b, b_iid, MAILBOX)  # no IntegrityError
    async with tenant_session(a) as s:
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX_CAPS) == b


# --------------------------------------------------------------------------- #
# the race (test_claim_hardening pattern, for email)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_concurrent_mailbox_claims_spelled_differently_admit_exactly_one_tenant():
    """Two tenants race one mailbox through a real claim point (``provision_email``),
    one typing it in capitals. The per-destination advisory lock (keyed on the
    lower-cased value) serializes them; the loser is refused by the case-folded
    gate — or, if its transaction saw no holder, by the index. Never two claims."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)

    async def claim(admin: uuid.UUID, iid: uuid.UUID, address: str) -> str:
        try:
            async with tenant_session(admin) as s:
                await service.provision_email(
                    s,
                    admin_id=admin,
                    instance_id=iid,
                    mode="own_domain",
                    email_address=address,
                    instance_name="Mail Co",
                    user_id=None,
                )
                return "claimed"
        except (EmailAddressTaken, IntegrityError):
            return "refused"

    outcomes = await asyncio.gather(claim(a, a_iid, MAILBOX), claim(b, b_iid, MAILBOX_CAPS))
    assert sorted(outcomes) == ["claimed", "refused"]

    # Exactly one live claim, and the resolver names that tenant for either spelling.
    rows = [row for row in (await _email_row(a), await _email_row(b)) if row is not None]
    assert len(rows) == 1
    winner = rows[0].admin_id
    async with tenant_session(winner) as s:
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX) == winner
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX_CAPS) == winner
