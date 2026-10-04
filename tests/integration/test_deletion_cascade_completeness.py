"""Tenant deletion must reach every tenant-scoped table (BE P0-9).

``grep -rIn 'ForeignKey|FOREIGN KEY'`` over the models and migrations returns nothing:
the schema declares no foreign keys at all. So Postgres will not cascade on our behalf
and will not error when a table is forgotten — a missed table is silent, and leaves a
deleted tenant's rows sitting in a live database under an admin_id nobody will ever
query again. Six tables had already been missed when this test was written.

The completeness check therefore walks ``information_schema`` rather than the model
registry: it asks the DATABASE which tables carry an ``admin_id``, so a table created by
a raw-SQL migration with no ORM model is caught too. This test is the only backstop
there is.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from app.connections.secrets import LocalSecretStore, secret_ref_for, tenant_secret_prefix
from app.db.session import tenant_session, untenanted_session
from app.lifecycle.cascade import CASCADE_EXEMPT_TABLES, CASCADE_MODELS, hard_delete_cascade
from app.models.audit import AdminAuditLog
from app.models.instance import Account, Instance
from sqlalchemy import Date, DateTime, Enum, Float, Integer, String, Text, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

pytestmark = pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")

_CASCADE_TABLES = frozenset(model.__tablename__ for model, _ in CASCADE_MODELS)

_TENANT_TABLES_SQL = text(
    "SELECT table_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND column_name = 'admin_id'"
)
_ALL_TABLES_SQL = text(
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
)


async def _table_names(sql) -> set[str]:
    async with untenanted_session() as s:
        return set((await s.execute(sql)).scalars())


async def test_every_tenant_scoped_table_is_deleted_or_deliberately_exempt():
    """The registry check. Adding a tenant table without adding it to CASCADE_MODELS is
    the failure mode, and nothing else in the stack would notice."""
    tenant_tables = await _table_names(_TENANT_TABLES_SQL)
    assert tenant_tables, "precondition: the schema must have tenant-scoped tables"

    missed = tenant_tables - _CASCADE_TABLES - CASCADE_EXEMPT_TABLES
    assert not missed, (
        f"tables carry admin_id but tenant deletion never touches them: {sorted(missed)}. "
        "Add them to CASCADE_MODELS, or to CASCADE_EXEMPT_TABLES with a reason."
    )


async def test_the_cascade_list_has_no_stale_entries():
    """The mirror: a table renamed or dropped by a migration must not linger in
    CASCADE_MODELS, where it would raise part-way through a deletion."""
    existing = await _table_names(_ALL_TABLES_SQL)
    assert _CASCADE_TABLES <= existing, sorted(_CASCADE_TABLES - existing)
    assert CASCADE_EXEMPT_TABLES <= existing, sorted(CASCADE_EXEMPT_TABLES - existing)


async def test_deleting_a_tenant_empties_every_cascade_table():
    """End to end: one row per cascade table for two tenants, delete one, and require
    that every table came back empty for them — and untouched for the other tenant."""
    doomed, neighbour = uuid.uuid4(), uuid.uuid4()
    await _seed(doomed)
    await _seed(neighbour)

    async with tenant_session(doomed) as s:
        counts = await hard_delete_cascade(s, doomed, include_account_scope=True)

    assert set(counts) == _CASCADE_TABLES, "cascade did not report on every table it owns"
    untouched = sorted(table for table, n in counts.items() if n == 0)
    assert not untouched, f"seeded tables the cascade deleted nothing from: {untouched}"

    for model, _ in CASCADE_MODELS:
        async with tenant_session(doomed) as s:
            left = (
                await s.execute(
                    select(func.count()).select_from(model).where(model.admin_id == doomed)
                )
            ).scalar_one()
            assert left == 0, f"{model.__tablename__} survived deletion"
        async with tenant_session(neighbour) as s:
            kept = (
                await s.execute(
                    select(func.count()).select_from(model).where(model.admin_id == neighbour)
                )
            ).scalar_one()
            assert kept == 1, f"{model.__tablename__} lost another tenant's row"


async def test_the_account_shell_and_audit_log_survive():
    """The exemptions are load-bearing, not an oversight: the shell is the closure flow's
    to destroy, and the audit log is the proof the deletion happened (§3.6.5)."""
    admin = uuid.uuid4()
    await _seed(admin)
    async with tenant_session(admin) as s:
        await hard_delete_cascade(s, admin, include_account_scope=True)
    async with tenant_session(admin) as s:
        assert (
            await s.execute(select(Account).where(Account.id == admin))
        ).scalar_one_or_none() is not None
        audited = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin)
            )
        ).scalar_one()
        assert audited > 0


async def test_the_luciel_scope_keeps_the_billing_identity():
    """F014/F019: the Luciel cascade (grace expiry) must not take the account's billing
    identity with it — the card, the Stripe customer and the lifetime count belong to the
    shell that survives. Only the account purge (``include_account_scope``) removes them."""
    from app.lifecycle.cascade import ACCOUNT_SCOPE_MODELS, LUCIEL_SCOPE_MODELS

    admin = uuid.uuid4()
    await _seed(admin)
    async with tenant_session(admin) as s:
        counts = await hard_delete_cascade(s, admin)
    assert set(counts) == {m.__tablename__ for m, _ in LUCIEL_SCOPE_MODELS}
    for model, _ in ACCOUNT_SCOPE_MODELS:
        async with tenant_session(admin) as s:
            kept = (
                await s.execute(
                    select(func.count()).select_from(model).where(model.admin_id == admin)
                )
            ).scalar_one()
        assert kept == 1, f"{model.__tablename__} was destroyed by a Luciel-scope cascade"


async def test_orphaned_tenant_secrets_are_swept_by_prefix(monkeypatch):
    """Per-``secret_ref`` deletion only reaches secrets a surviving row still names. A
    credential whose connection row was deleted earlier has nothing left in Postgres to
    point at it, so without a prefix sweep it lives in Secrets Manager forever."""
    import app.connections.secrets as secrets_module

    LocalSecretStore.reset()
    store = LocalSecretStore()
    monkeypatch.setattr(secrets_module, "_default", store)

    doomed, neighbour = uuid.uuid4(), uuid.uuid4()
    await _seed(doomed)
    await _seed(neighbour)

    orphan = f"vm/{doomed}/{uuid.uuid4()}/google_drive"
    await store.put(orphan, {"token": "orphaned"})
    neighbour_ref = secret_ref_for(neighbour, uuid.uuid4(), "notion")
    await store.put(neighbour_ref, {"token": "not yours"})

    async with tenant_session(doomed) as s:
        await hard_delete_cascade(s, doomed)

    assert await store.get(orphan) is None, "a secret no surviving row named was left behind"
    assert await store.get(neighbour_ref) is not None, "the sweep crossed a tenant boundary"
    assert not [k for k in LocalSecretStore._store if k.startswith(tenant_secret_prefix(doomed))]


# --- generic seeding ---------------------------------------------------------------- #
# Reflective rather than hand-written on purpose: a table added later is seeded without
# anyone remembering to edit this file. Otherwise the completeness test above would keep
# passing while the behavioural one quietly stopped covering the new table.
def _value_for(column, *, admin_id: uuid.UUID, instance_id: uuid.UUID) -> object:
    if column.name == "admin_id":
        return admin_id
    if column.name == "instance_id":
        return instance_id
    kind = column.type
    if isinstance(kind, PGUUID):
        return uuid.uuid4()
    if isinstance(kind, Enum):
        return kind.enums[0]
    if isinstance(kind, JSONB):
        return {}
    if isinstance(kind, DateTime):
        return dt.datetime.now(dt.UTC)
    if isinstance(kind, Date):
        return dt.date(2026, 1, 1)
    if isinstance(kind, Float):
        return 1.0
    if isinstance(kind, Integer):
        return 1
    if isinstance(kind, String | Text):
        value = f"seed-{uuid.uuid4().hex[:8]}"
        # A short column (escalation_contact_confirmations.kind is varchar(8), round 7)
        # gets a value that fits rather than a truncation error from Postgres.
        length = getattr(kind, "length", None)
        return value if not length or len(value) <= length else uuid.uuid4().hex[:length]
    raise AssertionError(
        f"{column.table.name}.{column.name} has type {type(kind).__name__}, which this "
        "seeder cannot fill — teach it rather than dropping the column from coverage."
    )


def _instantiate(model, *, admin_id: uuid.UUID, instance_id: uuid.UUID):
    kwargs = {
        column.key: _value_for(column, admin_id=admin_id, instance_id=instance_id)
        for column in model.__table__.columns
        if column.default is None
        and column.server_default is None
        and (not column.nullable or column.primary_key)
    }
    return model(**kwargs)


async def _seed(admin_id: uuid.UUID) -> None:
    """One account, one instance, and exactly one row in every cascade table."""
    instance_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@cascade.test",
                email_lower=f"{admin_id}@cascade.test",
                password_hash="x",
            )
        )
        s.add(Instance(id=instance_id, admin_id=admin_id, name=f"Luciel {admin_id.hex[:6]}"))
        for model, _ in CASCADE_MODELS:
            if model is not Instance:
                s.add(_instantiate(model, admin_id=admin_id, instance_id=instance_id))
        await s.flush()
