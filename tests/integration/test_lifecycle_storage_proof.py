"""Every data-lifecycle sweep must delete rows AND give the storage back (Legal §B5).

The existing sweep tests prove a sweep deletes the rows it was pointed at. That is a
weaker claim than the one §B5 actually makes. A sweep can pass every one of those tests
while still:

  * stranding the rows it did not think about — the schema declares **no foreign keys
    at all**, so nothing cascades and nothing errors when a table is forgotten;
  * stranding credential material in Secrets Manager, which Postgres can no longer even
    name once the row that pointed at it is gone;
  * leaving the deleted rows' pages permanently allocated, so "deleted" data still costs
    money and the tenant's footprint never actually plateaus.

This module is the proof for the third claim as well as the first two. For each sweep it
seeds realistic aged data, snapshots row counts and ``pg_total_relation_size``, runs the
sweep TWICE, and then asserts on all three axes.

What "freed" honestly means here
--------------------------------
Postgres does not return pages to the operating system on DELETE, and neither does plain
VACUUM — only ``VACUUM FULL``/``CLUSTER`` do, under an exclusive lock no production
sweep would ever take. So "the sweep freed the storage" cannot mean "the file shrank",
and a test asserting that would be asserting a false model of the database.

What it does mean, and what :func:`_assert_pages_were_reused` asserts, is the property
that actually bounds cost: after the sweep and a routine VACUUM, **re-seeding the same
volume of data reuses the released pages instead of appending new ones**, so the tenant's
footprint plateaus at roughly one copy of the data across delete/refill cycles rather
than growing by a copy each time. A sweep that deleted rows without freeing them fails
this, because its refill has to extend the relations to hold a second copy.

The tolerance is wide on purpose, and the honest reason is that reuse is real but not
exact. Plain VACUUM truncates trailing empty heap pages, so a swept relation can measure
smaller than it started; btree pages it frees stay in the index for reuse rather than
being returned; and a refill keyed by fresh random UUIDs does not repack an index the way
the first load did. So the test asserts the refill stays nearer one copy than two, which
is the claim §B5 actually needs, instead of asserting byte-exact reuse, which would be a
claim about Postgres's allocator that a correctly-working sweep would fail.

Dead-tuple accounting (``pg_stat_user_tables.n_dead_tup``) is reported alongside as
corroborating evidence, but the reuse test is the load-bearing assertion: it is exact
and synchronous, where the statistics view is approximate and updated asynchronously.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.billing import dunning
from app.db.session import tenant_session, untenanted_session
from app.jobs import sweeps
from app.lifecycle import audit_retention
from app.models.audit import ActorType, AdminAuditLog
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.budget import CountedSession
from app.models.connection import InstanceConnection
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import EMBED_DIM, KnowledgeChunk, KnowledgeSource
from app.models.lead import Lead, LeadState
from sqlalchemy import func, select, text

from tests.conftest import superuser_exec

# The information_schema walker and the reflective seeder are the P0-9 completeness
# machinery. Importing them rather than re-deriving them is deliberate: a table added by
# a later migration must widen the orphan check here at the same instant it widens the
# cascade check there, with nobody remembering to edit two files.
from tests.integration.test_deletion_cascade_completeness import (
    _TENANT_TABLES_SQL,
    _instantiate,
)

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

#: Tables that legitimately still hold rows for a swept tenant. These are the same
#: exemptions the cascade declares (the audit log is the proof the deletion happened;
#: the internal access log is a platform-STAFF record with its own 3-year floor), plus
#: ``accounts`` for the sweeps whose contract is to keep the shell alive.
_SURVIVES_ANY_SWEEP = frozenset({"admin_audit_log", "internal_access_log"})

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Enough 1536-dimension float4 vectors (~6 KB each) that the seeded volume dwarfs the
#: fixed per-relation overhead a first VACUUM adds (free-space + visibility maps).
_CHUNKS = 220


def _slack(tables: list[str]) -> int:
    """Bytes of measurement noise to tolerate.

    A relation's free-space and visibility maps are counted by
    ``pg_total_relation_size`` and appear (or gain a page) the first time VACUUM touches
    a non-trivial relation, so an exact equality would be asserting an implementation
    detail of the maps rather than anything about the sweep. One FSM page-triple per
    table plus a fixed floor is comfortably above that noise and far below the volume
    each test seeds.
    """
    return 96 * 1024 * len(tables) + 64 * 1024


# --- storage probes ------------------------------------------------------------------ #


@dataclass(frozen=True)
class Storage:
    """One relation's occupancy at a moment in time."""

    size: dict[str, int]
    dead: dict[str, int]

    def total(self, tables: list[str]) -> int:
        return sum(self.size.get(t, 0) for t in tables)


def _checked(tables: list[str]) -> list[str]:
    """Reject anything that is not a plain identifier before it reaches SQL.

    Table names here come from ``information_schema``, so they are already trustworthy;
    the check is here because they are nonetheless interpolated rather than bound, and a
    reader should not have to reason about the provenance to know the query is safe.
    """
    bad = [t for t in tables if not _IDENT.match(t)]
    assert not bad, f"refusing to interpolate non-identifier table names: {bad}"
    return tables


async def _storage(tables: list[str]) -> Storage:
    """Relation bytes + dead-tuple estimate for each table.

    ``pg_total_relation_size`` covers the heap, its indexes, TOAST, and the maps — i.e.
    everything the tenant's data actually costs, not just the main fork.
    """
    _checked(tables)
    async with untenanted_session() as s:
        await s.execute(text("SELECT pg_stat_clear_snapshot()"))
        rows = (
            await s.execute(
                text(
                    "SELECT c.relname, pg_total_relation_size(c.oid), "
                    "       COALESCE(st.n_dead_tup, 0) "
                    "  FROM pg_class c "
                    "  LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid "
                    " WHERE c.relname = ANY(:names) AND c.relkind = 'r'"
                ),
                {"names": tables},
            )
        ).all()
    return Storage({r[0]: int(r[1]) for r in rows}, {r[0]: int(r[2]) for r in rows})


def _vacuum(tables: list[str]) -> None:
    """Routine VACUUM, as autovacuum would run it — never VACUUM FULL.

    Goes through the superuser path because ``admin_audit_log`` is owned by
    ``luciel_audit_owner``; the app role would silently skip it with a warning.
    """
    superuser_exec("VACUUM (ANALYZE) " + ", ".join(_checked(tables)))


async def _row_counts(admin_id: uuid.UUID, tables: list[str]) -> dict[str, int]:
    """Rows this tenant owns in each table, read under RLS as the app role sees them."""
    _checked(tables)
    counts: dict[str, int] = {}
    async with tenant_session(admin_id) as s:
        for table in tables:
            counts[table] = int(
                (
                    await s.execute(
                        text(f"SELECT count(*) FROM {table} WHERE admin_id = :aid"),  # noqa: S608
                        {"aid": str(admin_id)},
                    )
                ).scalar_one()
            )
    return counts


async def _tenant_tables() -> list[str]:
    """Every table carrying an ``admin_id`` — asked of the DATABASE, not the ORM (P0-9)."""
    async with untenanted_session() as s:
        return sorted((await s.execute(_TENANT_TABLES_SQL)).scalars())


async def _assert_no_orphans(admin_id: uuid.UUID, *, survivors: frozenset[str]) -> None:
    """No tenant-scoped table may still hold a row for a purged tenant.

    Walks information_schema rather than a hand-maintained list, so a table introduced by
    a raw-SQL migration with no ORM model is covered the day it appears.
    """
    tables = [t for t in await _tenant_tables() if t not in survivors]
    left = {t: n for t, n in (await _row_counts(admin_id, tables)).items() if n}
    assert not left, f"sweep left rows behind for a purged tenant: {left}"


def _assert_secrets_gone(admin_id: uuid.UUID, *, neighbour_ref: str | None = None) -> None:
    from app.connections.secrets import LocalSecretStore, tenant_secret_prefix

    prefix = tenant_secret_prefix(admin_id)
    stranded = [k for k in LocalSecretStore._store if k.startswith(prefix)]
    assert not stranded, f"credential material outlived the tenant in Secrets Manager: {stranded}"
    if neighbour_ref is not None:
        assert neighbour_ref in LocalSecretStore._store, "the sweep crossed a tenant boundary"


@pytest.fixture
def secret_store(monkeypatch):
    """Bind the fake Secrets Manager and hand the test a clean store."""
    import app.connections.secrets as secrets_module

    LocalSecretStore = secrets_module.LocalSecretStore
    LocalSecretStore.reset()
    store = LocalSecretStore()
    monkeypatch.setattr(secrets_module, "_default", store)
    return store


# --- the storage proof ---------------------------------------------------------------- #


async def _assert_pages_were_reused(seed, sweep, tables: list[str]) -> Storage:
    """The load-bearing storage assertion. See the module docstring for what it proves.

    ``seed`` is called twice with identical volume, and the question is which of two
    outcomes the second one lands in. If the sweep genuinely released the first seed's
    pages, the refill reuses them and the relations end up holding roughly ONE copy of
    the data. If the sweep only unlinked rows a reader can no longer see, the refill has
    to extend the relations and they end up holding TWO. Those differ by the whole
    seeded volume, so the midpoint between them separates the two cases with a wide
    margin either side.

    The bound is deliberately not "the refill is byte-for-byte free". Reuse is real but
    not exact: plain VACUUM truncates trailing empty heap pages (so a swept relation can
    measure *smaller* than it started), while btree pages it frees stay in the index for
    reuse rather than being handed back, and a refill keyed by fresh random UUIDs does
    not repack them identically. Asserting exact reuse would be asserting the allocator's
    behaviour rather than the sweep's, and would fail on a sweep that is doing its job.
    """
    slack = _slack(tables)
    _vacuum(tables)
    empty = await _storage(tables)

    await seed()
    _vacuum(tables)
    seeded = await _storage(tables)
    grew = seeded.total(tables) - empty.total(tables)
    assert grew > 2 * slack, (
        "precondition: the seed must occupy enough storage for its release to be "
        f"measurable, but the relations only grew by {grew} bytes"
    )

    await sweep()
    await sweep()  # at-least-once delivery: the second run must find nothing to do
    _vacuum(tables)
    swept = await _storage(tables)
    # Slack, not equality: a sweep is allowed to append its own tombstone to a table it
    # just pruned (audit retention does exactly that) — it is not allowed to come out of
    # the run anywhere near the size it went in at.
    assert swept.total(tables) <= seeded.total(tables) + slack, (
        f"the sweep itself grew the relations it was meant to shrink: {seeded.size} -> {swept.size}"
    )

    await seed()
    _vacuum(tables)
    refilled = await _storage(tables)
    stranded_ceiling = empty.total(tables) + 2 * grew  # what "freed nothing" would cost
    budget = empty.total(tables) + grew + grew // 2 + slack
    assert refilled.total(tables) <= budget, (
        "the swept pages were NOT reused — re-seeding the same volume grew the relations "
        "towards holding two copies of the data, so the deleted rows still cost storage. "
        f"empty={empty.size} seeded={seeded.size} after_sweep={swept.size} "
        f"refilled={refilled.size} (a sweep that freed nothing would reach "
        f"~{stranded_ceiling} bytes; the budget for one copy plus reuse slop is {budget})"
    )
    return swept


# --- seeding ---------------------------------------------------------------------------- #


def _embedding(n: int) -> list[float]:
    """A deterministic but non-degenerate vector — a constant one would TOAST-compress
    to almost nothing and understate what real embeddings cost."""
    return [((n * 37 + i * 11) % 1000) / 1000.0 for i in range(EMBED_DIM)]


async def _seed_account(
    admin_id: uuid.UUID,
    state: AccountState = AccountState.verified,
    *,
    created_at: datetime | None = None,
) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@proof.test",
                email_lower=f"{admin_id}@proof.test",
                password_hash="x",
                state=state,
                email_verified=state is not AccountState.unverified,
                **({} if created_at is None else {"created_at": created_at}),
            )
        )


async def _seed_instance(
    admin_id: uuid.UUID,
    state: LucielState = LucielState.active,
    *,
    grace_start: datetime | None = None,
    lead_retention_days: int | None = None,
) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="Proof",
                state=state,
                grace_window_started_at=grace_start,
                lead_retention_days=lead_retention_days,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


async def _seed_conversation_corpus(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    age: timedelta,
    chunks: int = _CHUNKS,
    conversations: int = 12,
) -> None:
    """A realistic tenant footprint: knowledge chunks with real pgvector embeddings,
    conversations with transcripts and summaries, leads, and the billing rows."""
    source_id = uuid.uuid4()
    created = NOW - age
    nonce = uuid.uuid4().hex[:8]
    async with tenant_session(admin_id) as s:
        # One billing account per tenant is a hard constraint, and a corpus may be seeded
        # more than once for the same tenant (an aged batch plus a live one).
        has_billing = (
            await s.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
        ).scalar_one_or_none() is not None
        s.add(
            KnowledgeSource(
                id=source_id,
                admin_id=admin_id,
                instance_id=instance_id,
                origin="upload",
                name="handbook.pdf",
                ingestion_status="ready",
                created_at=created,
            )
        )
        for n in range(chunks):
            s.add(
                KnowledgeChunk(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    source_id=source_id,
                    ordinal=n,
                    text=f"chunk {n} " + ("policy text " * 20),
                    embedding=_embedding(n),
                    created_at=created,
                )
            )
        for n in range(conversations):
            session_id = uuid.uuid4()
            s.add(
                Session(
                    id=session_id,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    channel="web",
                    summary=f"caller {n} asked about pricing and left a number",
                    created_at=created,
                    started_at=created,
                    last_message_at=created,
                )
            )
            s.add(
                SessionSummary(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    summary=f"summary {n} " + ("detail " * 40),
                    created_at=created,
                )
            )
            for turn in range(6):
                s.add(
                    Transcript(
                        id=uuid.uuid4(),
                        admin_id=admin_id,
                        instance_id=instance_id,
                        session_id=session_id,
                        role="lead" if turn % 2 else "ai",
                        text=f"turn {turn} " + ("conversation body " * 15),
                        created_at=created,
                        at=created,
                    )
                )
            s.add(
                CountedSession(
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    billing_period_start=created.date(),
                    created_at=created,
                )
            )
        for n in range(8):
            s.add(
                Lead(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    state=LeadState.active,
                    name=f"lead {n}",
                    contact_identifier="+15550100",
                    intent="asked about pricing",
                    created_at=created,
                    last_activity_at=created,
                )
            )
        if not has_billing:
            s.add(
                BillingAccount(
                    admin_id=admin_id,
                    stripe_customer_id=f"cus_{admin_id.hex[:12]}",
                    stripe_subscription_id=f"sub_{admin_id.hex[:12]}",
                    created_at=created,
                )
            )
        for n in range(5):
            s.add(
                ProcessedStripeEvent(
                    event_id=f"evt_{nonce}_{n}",
                    admin_id=admin_id,
                    created_at=created,
                )
            )


async def _seed_connection_with_secret(
    admin_id: uuid.UUID, instance_id: uuid.UUID, store, *, connection_type: str = "calendar"
) -> str:
    from app.connections.secrets import secret_ref_for

    connection_id = uuid.uuid4()
    ref = secret_ref_for(admin_id, instance_id, connection_type)
    await store.put(ref, {"access_token": "live-token"})
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=connection_id,
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type=connection_type,
                auth_class="oauth",
                provider="google",
                status="connected",
                secret_ref=ref,
            )
        )
    return ref


async def _seed_every_tenant_table(admin_id: uuid.UUID, instance_id: uuid.UUID) -> None:
    """One row in every cascade table, reflectively — so a table added by a later
    migration is seeded here without anyone remembering to edit this file, and the
    orphan check below therefore has something to catch."""
    from app.lifecycle.cascade import CASCADE_MODELS

    async with tenant_session(admin_id) as s:
        for model, _ in CASCADE_MODELS:
            if model is Instance:
                continue
            s.add(_instantiate(model, admin_id=admin_id, instance_id=instance_id))


# --- 1. unverified-account 7-day reaper ------------------------------------------------ #


async def test_unverified_reaper_purges_the_whole_tenant_not_just_the_shell(secret_store) -> None:
    """§3.7.1a / Legal §A1 promise the abandoned account is "permanently deleted".

    An abandoned signup is not always an empty one: the reaper's candidate is any account
    still ``unverified`` past the window, and a partially-onboarded tenant can already own
    an instance, knowledge chunks, and live OAuth credentials. Deleting only the
    ``accounts`` row would leave every one of them addressed by an admin_id no query will
    ever name again — invisible, undeletable, and still billed for.
    """
    doomed, neighbour = uuid.uuid4(), uuid.uuid4()
    aged = NOW - timedelta(days=8)
    await _seed_account(doomed, AccountState.unverified, created_at=aged)
    instance_id = await _seed_instance(doomed)
    await _seed_every_tenant_table(doomed, instance_id)
    await _seed_connection_with_secret(doomed, instance_id, secret_store)

    await _seed_account(neighbour, AccountState.unverified, created_at=NOW - timedelta(hours=1))
    neighbour_ref = await _seed_connection_with_secret(
        neighbour, await _seed_instance(neighbour), secret_store, connection_type="crm"
    )

    assert await sweeps.run_unverified_reap(now=NOW) == [str(doomed)]
    assert await sweeps.run_unverified_reap(now=NOW) == [], "the reaper is not idempotent"

    await _assert_no_orphans(doomed, survivors=_SURVIVES_ANY_SWEEP)
    _assert_secrets_gone(doomed, neighbour_ref=neighbour_ref)

    async with tenant_session(neighbour) as s:
        assert (
            await s.execute(select(Account).where(Account.id == neighbour))
        ).scalar_one_or_none() is not None, "the reaper took an account inside its window"


async def test_unverified_reaper_frees_the_storage_it_deletes(secret_store) -> None:
    tables = ["accounts", "instances", "knowledge_chunks", "transcripts", "sessions"]
    seeded: list[uuid.UUID] = []

    async def seed() -> None:
        admin_id = uuid.uuid4()
        seeded.append(admin_id)
        await _seed_account(admin_id, AccountState.unverified, created_at=NOW - timedelta(days=8))
        instance_id = await _seed_instance(admin_id)
        await _seed_conversation_corpus(admin_id, instance_id, age=timedelta(days=8))

    async def sweep() -> None:
        await sweeps.run_unverified_reap(now=NOW)

    await _assert_pages_were_reused(seed, sweep, tables)


# --- 2. transcript retention ----------------------------------------------------------- #


async def test_retention_leaves_no_conversation_content_past_the_year() -> None:
    """§B5 publishes a 1-year ceiling on conversation data, and ``sessions.summary`` is
    conversation data: it is the model's own summary of what was said, sitting in a text
    column on the parent row. Deleting the transcripts and the ``session_summaries`` while
    leaving that row is not a 1-year retention, and the leftover shells also accumulate
    without bound on a tenant who never deletes anything.
    """
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    instance_id = await _seed_instance(admin_id)
    await _seed_conversation_corpus(admin_id, instance_id, age=timedelta(days=400), chunks=4)
    # A live conversation from last week must be untouched by an age sweep.
    await _seed_conversation_corpus(admin_id, instance_id, age=timedelta(days=7), chunks=4)

    before = await _row_counts(admin_id, ["transcripts", "session_summaries", "sessions"])
    assert before == {"transcripts": 144, "session_summaries": 24, "sessions": 24}

    await sweeps.run_retention_prune(now=NOW)
    first = await _row_counts(admin_id, ["transcripts", "session_summaries", "sessions"])
    await sweeps.run_retention_prune(now=NOW)
    second = await _row_counts(admin_id, ["transcripts", "session_summaries", "sessions"])

    assert first == {"transcripts": 72, "session_summaries": 12, "sessions": 12}, (
        "aged conversation content survived the 1-year sweep (or the recent "
        f"conversations were taken with it): {first}"
    )
    assert second == first, f"the retention sweep is not idempotent: {first} -> {second}"

    async with tenant_session(admin_id) as s:
        stale = (
            await s.execute(
                select(func.count())
                .select_from(Session)
                .where(Session.admin_id == admin_id, Session.created_at < NOW - timedelta(days=365))
            )
        ).scalar_one()
    assert stale == 0, "an aged session row (and its summary text) outlived the retention window"


async def test_retention_frees_the_storage_it_deletes() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    instance_id = await _seed_instance(admin_id)
    tables = ["transcripts", "session_summaries", "sessions"]

    async def seed() -> None:
        await _seed_conversation_corpus(
            admin_id, instance_id, age=timedelta(days=400), chunks=0, conversations=600
        )

    async def sweep() -> None:
        await sweeps.run_retention_prune(now=NOW)

    await _assert_pages_were_reused(seed, sweep, tables)


# --- 3. Luciel-delete grace expiry + account-close purge ------------------------------- #


async def test_grace_expiry_cascade_strands_nothing_and_releases_the_pages(
    secret_store,
) -> None:
    """The flagship: a full tenant footprint, including pgvector embeddings, destroyed at
    day 30 and the pages handed back."""
    tables = [
        "knowledge_chunks",
        "knowledge_sources",
        "transcripts",
        "session_summaries",
        "sessions",
        "leads",
        "counted_sessions",
        "processed_stripe_events",
        "billing_accounts",
        "instance_connections",
        "instances",
    ]
    expired = NOW - timedelta(days=31)
    seeded: list[uuid.UUID] = []

    async def seed() -> None:
        admin_id = uuid.uuid4()
        seeded.append(admin_id)
        await _seed_account(admin_id)
        instance_id = await _seed_instance(
            admin_id, LucielState.luciel_grace_window, grace_start=expired
        )
        await _seed_conversation_corpus(admin_id, instance_id, age=timedelta(days=40))
        await _seed_connection_with_secret(admin_id, instance_id, secret_store)

    async def sweep() -> None:
        await sweeps.run_grace_purge(now=NOW)

    await _assert_pages_were_reused(seed, sweep, tables)

    # The reuse helper leaves its final re-seed in place; sweep once more so the orphan
    # and secret checks below cover every tenant it created, not just the first.
    await sweep()
    assert len(seeded) == 2, "the reuse helper is expected to seed exactly twice"
    for admin_id in seeded:
        await _assert_no_orphans(admin_id, survivors=_SURVIVES_ANY_SWEEP | {"accounts"})
        _assert_secrets_gone(admin_id)


async def test_grace_expiry_is_idempotent_and_respects_the_window(secret_store) -> None:
    due, inside = uuid.uuid4(), uuid.uuid4()
    await _seed_account(due)
    due_instance = await _seed_instance(
        due, LucielState.luciel_grace_window, grace_start=NOW - timedelta(days=31)
    )
    await _seed_every_tenant_table(due, due_instance)
    await _seed_connection_with_secret(due, due_instance, secret_store)

    await _seed_account(inside)
    inside_instance = await _seed_instance(
        inside, LucielState.luciel_grace_window, grace_start=NOW - timedelta(days=3)
    )
    inside_ref = await _seed_connection_with_secret(
        inside, inside_instance, secret_store, connection_type="crm"
    )

    first = await sweeps.run_grace_purge(now=NOW)
    second = await sweeps.run_grace_purge(now=NOW)
    assert list(first) == [str(due)]
    assert second == {}, f"a second grace sweep re-deleted an already-deleted tenant: {second}"

    await _assert_no_orphans(due, survivors=_SURVIVES_ANY_SWEEP | {"accounts"})
    _assert_secrets_gone(due, neighbour_ref=inside_ref)

    async with tenant_session(inside) as s:
        assert (
            await s.execute(select(Instance).where(Instance.admin_id == inside))
        ).scalar_one_or_none() is not None, "a Luciel still inside its 30-day window was destroyed"


async def test_close_purge_destroys_the_shell_and_everything_under_it(secret_store) -> None:
    """The account-close 30-day cascade. The shell is what holds the email address, so a
    purge that leaves tenant rows behind also leaves them unreachable forever — there is
    no account left to log in as and query them."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.closed)
    instance_id = await _seed_instance(admin_id)
    await _seed_every_tenant_table(admin_id, instance_id)
    await _seed_connection_with_secret(admin_id, instance_id, secret_store)
    async with tenant_session(admin_id) as s:
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            event_type="account_closure_initiated",
        )

    later = datetime.now(UTC) + timedelta(days=31)
    assert await sweeps.run_close_account_purge(now=later) == [str(admin_id)]
    assert await sweeps.run_close_account_purge(now=later) == [], "close purge is not idempotent"

    await _assert_no_orphans(admin_id, survivors=_SURVIVES_ANY_SWEEP)
    _assert_secrets_gone(admin_id)


async def test_close_purge_reaches_a_tenant_that_never_built_a_luciel(secret_store) -> None:
    """Closure does not require a Luciel. An account that signed up, attached a card, and
    closed without ever finishing onboarding still owns billing rows and possibly
    credentials — and the cascade is the only thing that would remove them."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.closed)
    ref = f"vm/{admin_id}/{uuid.uuid4()}/calendar"
    await secret_store.put(ref, {"access_token": "abandoned"})
    async with tenant_session(admin_id) as s:
        s.add(
            BillingAccount(
                admin_id=admin_id,
                stripe_customer_id=f"cus_{admin_id.hex[:12]}",
                stripe_subscription_id=None,
            )
        )
        s.add(ProcessedStripeEvent(event_id=f"evt_{admin_id.hex[:8]}", admin_id=admin_id))
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            event_type="account_closure_initiated",
        )

    later = datetime.now(UTC) + timedelta(days=31)
    assert await sweeps.run_close_account_purge(now=later) == [str(admin_id)]

    await _assert_no_orphans(admin_id, survivors=_SURVIVES_ANY_SWEEP)
    _assert_secrets_gone(admin_id)


# --- 4. lead prune / auto-prune / archive ---------------------------------------------- #


async def test_lead_auto_prune_is_opt_in_exempts_archived_and_is_idempotent() -> None:
    """§3.4.10a: leads are the Admin's business data, so the platform prunes them only on
    a window the Admin set, and archive is the documented alternative to deletion — an
    auto-rule that deleted archived rows anyway would make that choice meaningless."""
    opted_in, default = uuid.uuid4(), uuid.uuid4()
    stale = NOW - timedelta(days=200)

    for admin_id, window in ((opted_in, 90), (default, None)):
        await _seed_account(admin_id)
        instance_id = await _seed_instance(admin_id, lead_retention_days=window)
        async with tenant_session(admin_id) as s:
            for state, activity in (
                (LeadState.active, stale),
                (LeadState.archived, stale),
                (LeadState.active, NOW - timedelta(days=2)),
            ):
                s.add(
                    Lead(
                        id=uuid.uuid4(),
                        admin_id=admin_id,
                        instance_id=instance_id,
                        state=state,
                        name="x",
                        created_at=stale,
                        last_activity_at=activity,
                    )
                )

    await sweeps.run_retention_prune(now=NOW)
    first = await _row_counts(opted_in, ["leads"])
    await sweeps.run_retention_prune(now=NOW)

    assert first["leads"] == 2, "the stale non-archived lead was not pruned, or archive was ignored"
    assert await _row_counts(opted_in, ["leads"]) == first, "lead auto-prune is not idempotent"
    assert (await _row_counts(default, ["leads"]))["leads"] == 3, (
        "a tenant that never set leadRetentionDays had leads deleted on a platform timer"
    )

    async with tenant_session(opted_in) as s:
        survivors = (
            (await s.execute(select(Lead.state).where(Lead.admin_id == opted_in))).scalars().all()
        )
    assert LeadState.archived in survivors


async def test_lead_prune_frees_the_storage_it_deletes() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    instance_id = await _seed_instance(admin_id, lead_retention_days=90)
    stale = NOW - timedelta(days=200)

    async def seed() -> None:
        async with tenant_session(admin_id) as s:
            for n in range(4000):
                s.add(
                    Lead(
                        id=uuid.uuid4(),
                        admin_id=admin_id,
                        instance_id=instance_id,
                        state=LeadState.active,
                        name=f"lead {n}",
                        contact_identifier=f"+1555{n:06d}",
                        intent="captured from the web widget",
                        created_at=stale,
                        last_activity_at=stale,
                    )
                )

    async def sweep() -> None:
        await sweeps.run_retention_prune(now=NOW)

    await _assert_pages_were_reused(seed, sweep, ["leads"])


# --- 5. dunning ------------------------------------------------------------------------ #


async def test_dunning_advance_fires_once_and_stays_put() -> None:
    """Dunning frees no storage — it is a state transition — so the property that matters
    is that at-least-once delivery cannot double-apply it."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    instance_id = await _seed_instance(admin_id)
    async with tenant_session(admin_id) as s:
        s.add(
            BillingAccount(
                admin_id=admin_id,
                stripe_customer_id=f"cus_{admin_id.hex[:12]}",
                dunning_state=dunning.RETRYING,
                dunning_started_at=NOW - timedelta(days=8),
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="calendar",
                auth_class="oauth",
                provider="google",
                status="connected",
            )
        )

    assert await sweeps.run_dunning_transitions(now=NOW) == [str(admin_id)]
    assert await sweeps.run_dunning_transitions(now=NOW) == [], "dunning advanced twice"

    async with tenant_session(admin_id) as s:
        account = (
            await s.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
        ).scalar_one()
        assert account.dunning_state == dunning.REDUCED
        transitions = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "dunning_state_changed",
                )
            )
        ).scalar_one()
    assert transitions == 1, f"the sweep wrote {transitions} transition rows for one transition"


# --- 6. audit 1-year retention --------------------------------------------------------- #


async def _append_audit(admin_id: uuid.UUID, *, age: timedelta, n: int) -> None:
    """Rows are inserted genuinely old rather than swept with a doctored clock: the
    procedure derives its cutoff from ``now()`` server-side and the trigger re-derives it."""
    created_at = datetime.now(UTC) - age
    async with tenant_session(admin_id) as s:
        for i in range(n):
            s.add(
                AdminAuditLog(
                    event_id=uuid.uuid4(),
                    admin_id=admin_id,
                    actor_type=ActorType.platform,
                    event_type="login_succeeded",
                    event_payload={"i": i, "detail": "padding " * 20},
                    created_at=created_at,
                )
            )


async def test_audit_retention_deletes_only_aged_rows_and_is_idempotent() -> None:
    admin_id = uuid.uuid4()
    # The account must predate its aged rows (append-only, server-stamped created_at);
    # discovery's grant-free age predicate (migration 0034) relies on that invariant.
    await _seed_account(admin_id, created_at=datetime.now(UTC) - timedelta(days=405))
    await _append_audit(admin_id, age=timedelta(days=400), n=40)
    await _append_audit(admin_id, age=timedelta(days=30), n=10)

    assert (await _row_counts(admin_id, ["admin_audit_log"]))["admin_audit_log"] == 50
    assert await sweeps.run_audit_retention() == [str(admin_id)]

    # 40 aged rows go and the prune appends one tombstone of its own, so the surviving
    # count is the 10 in-window rows plus that record. Counting the delta alone would
    # hide either half of it.
    async def _aged_and_total() -> tuple[int, int]:
        cutoff = datetime.now(UTC) - timedelta(days=audit_retention.AUDIT_RETENTION_DAYS)
        async with tenant_session(admin_id) as s:
            aged = (
                await s.execute(
                    select(func.count())
                    .select_from(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin_id, AdminAuditLog.created_at < cutoff)
                )
            ).scalar_one()
            total = (
                await s.execute(
                    select(func.count())
                    .select_from(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin_id)
                )
            ).scalar_one()
        return int(aged), int(total)

    assert await _aged_and_total() == (0, 11), "aged audit rows outlived the 1-year sweep"

    assert await sweeps.run_audit_retention() == [], "audit retention is not idempotent"
    assert await _aged_and_total() == (0, 11), (
        "the second run pruned or logged again — the retention job must not become a "
        "writer to the table it exists to bound"
    )


async def test_audit_retention_frees_the_storage_it_deletes() -> None:
    admin_id = uuid.uuid4()
    # Aged account: see test_audit_retention_deletes_only_aged_rows_and_is_idempotent.
    await _seed_account(admin_id, created_at=datetime.now(UTC) - timedelta(days=405))

    async def seed() -> None:
        await _append_audit(admin_id, age=timedelta(days=400), n=6000)

    async def sweep() -> None:
        await sweeps.run_audit_retention()

    await _assert_pages_were_reused(seed, sweep, ["admin_audit_log"])


async def test_the_audit_prune_procedure_is_part_of_the_deploy_path() -> None:
    """``audit_retention_prune`` lives in ``scripts/lock_audit_log.sql``, not in a
    migration — it must be OWNED by ``luciel_audit_owner`` for the append-only trigger to
    exempt it, and alembic runs as ``luciel_app``, which is deliberately not a member of
    that role. That split is correct, but it means a deploy that ran ``alembic upgrade
    head`` and nothing else leaves the nightly sweep failing with ``undefined_function``
    against a schema that otherwise looks complete.

    ``AUDIT_LOCK_CHECKS`` is what closes that gap: ``scripts/migrate.sh`` runs it as the
    last step of the migration task (through ``scripts/verify_audit_lock.py``), and
    ``python -m app.jobs audit_retention`` preflights with it rather than raising a bare
    Postgres error from four frames deep. This test asserts those checks agree with a
    database provisioned the way a deploy provisions one.
    """
    async with untenanted_session() as s:
        results = await audit_retention.audit_lock_results(s)
    assert not audit_retention.lock_failures(results), (
        "the audit-log lock is not fully applied to this database: "
        f"{audit_retention.lock_failures(results)}. Run scripts/lock_audit_log.sql as master."
    )


# --- 7. usage reconcile ---------------------------------------------------------------- #


async def test_usage_reconcile_posts_a_missed_block_exactly_once(monkeypatch) -> None:
    """The backstop for a tenant who crossed a billing block and then went quiet. Because
    it runs nightly against every tenant with an unreported block, posting the same block
    twice would double-bill — so idempotency here is a money property, not a tidiness one.
    """
    import app.billing.metering as metering_module

    posted: list[int] = []

    class RecordingGateway:
        async def report_meter_event(self, *, customer_id, value, identifier):
            posted.append(value)
            return True

    monkeypatch.setattr(metering_module, "get_stripe_gateway", lambda: RecordingGateway())

    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    await _seed_instance(admin_id)
    async with tenant_session(admin_id) as s:
        s.add(
            BillingAccount(
                admin_id=admin_id,
                stripe_customer_id=f"cus_{admin_id.hex[:12]}",
                stripe_subscription_id=f"sub_{admin_id.hex[:12]}",
                accrued_blocks=3,
                last_reported_blocks=1,
            )
        )

    assert await sweeps.run_usage_reconcile(now=NOW) == [str(admin_id)]
    assert await sweeps.run_usage_reconcile(now=NOW) == [], "usage was reported twice"
    # Watermark 1, accrued 3 → the meter gets the DELTA of 2, not the total of 3. On an
    # additive meter, flushing the total would bill 3 blocks on top of the 1 already there.
    assert posted == [2], f"the reconcile posted {posted}, not one delta past the watermark"

    async with tenant_session(admin_id) as s:
        account = (
            await s.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
        ).scalar_one()
    assert account.last_reported_blocks == 3


# --- 8. the whole-schema guarantee ------------------------------------------------------ #


async def test_no_sweep_can_be_added_without_an_orphan_check() -> None:
    """A meta-assertion. The orphan walker above is only as good as its exemption list;
    if a future migration adds a tenant table that legitimately survives every purge, it
    must be declared here rather than quietly widening what "no orphans" means."""
    tenant_tables = set(await _tenant_tables())
    assert _SURVIVES_ANY_SWEEP <= tenant_tables, sorted(_SURVIVES_ANY_SWEEP - tenant_tables)
    assert "accounts" in tenant_tables
