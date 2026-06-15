"""Layer-2 tenant isolation: PostgreSQL Row-Level Security policy generation.

Every tenant table (any model mixing in :class:`TenantMixin`) gets:
  - ``ENABLE ROW LEVEL SECURITY`` and ``FORCE ROW LEVEL SECURITY`` (so even the
    table owner is subject to RLS — without FORCE, the owning role bypasses it).
  - a ``tenant_isolation`` policy using ``admin_id = current_setting('app.admin_id')::uuid``.

The append-only audit log additionally gets INSERT-only semantics: a policy that
permits SELECT/INSERT for the tenant but no UPDATE/DELETE, backed by table grants
(Architecture §5.3). UPDATE/DELETE are granted to no application role.

Generation is driven off the ORM metadata so a newly-added tenant table is
covered automatically — there is no hand-maintained table list to drift.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.db.base import Base, TenantMixin

APP_GUC = "app.admin_id"

# Tables that are append-only (audit log). INSERT + SELECT only; never UPDATE/DELETE.
APPEND_ONLY_TABLES: frozenset[str] = frozenset({"admin_audit_log"})


def tenant_table_names() -> list[str]:
    """All mapped tables whose model mixes in TenantMixin."""
    names: list[str] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, TenantMixin):
            names.append(cls.__tablename__)  # type: ignore[attr-defined]
    return sorted(set(names))


def rls_statements_for_table(table: str) -> list[str]:
    """SQL statements that install the three-layer Layer-2 defense for one table."""
    stmts = [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        # FORCE makes the policy apply to the table owner too (the app role owns the schema).
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS tenant_isolation ON {table};",
    ]
    # NULLIF + missing_ok=true → unset GUC yields NULL → zero rows (safe default).
    predicate = f"admin_id = NULLIF(current_setting('{APP_GUC}', true), '')::uuid"
    stmts.append(
        f"CREATE POLICY tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate});"
    )
    return stmts


def grant_statements_for_table(table: str, role: str) -> list[str]:
    """Table grants. Append-only tables get INSERT+SELECT only (Architecture §5.3)."""
    if table in APPEND_ONLY_TABLES:
        return [
            f"REVOKE ALL ON {table} FROM {role};",
            f"GRANT SELECT, INSERT ON {table} TO {role};",
        ]
    return [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role};"]


def all_rls_statements(tables: Iterable[str] | None = None) -> list[str]:
    tabs = list(tables) if tables is not None else tenant_table_names()
    out: list[str] = []
    for t in tabs:
        out.extend(rls_statements_for_table(t))
    return out
