"""TRUNCATE guards on both append-only audit artifacts (§5.3 / §5.11)

Audit finding B4: the append-only commitment for ``internal_access_log`` (§5.11 /
Commitment C23) did not hold. Migration 0012 revoked only ``UPDATE, DELETE`` and
installed a row-level ``BEFORE UPDATE OR DELETE`` trigger, which leaves three holes:

  1. TRUNCATE is a privilege of its own — ``REVOKE UPDATE, DELETE`` does not touch it;
  2. TRUNCATE is inherent to the table OWNER, and 0012 ran as ``luciel_app``, so the
     app role owned the table and could TRUNCATE, re-GRANT, and DROP the guard;
  3. row-level triggers never fire on TRUNCATE — that needs a statement-level
     ``BEFORE TRUNCATE`` trigger.

This migration closes (1) and (3) on BOTH audit tables. It cannot close (2):
transferring ownership to ``luciel_audit_owner`` requires membership of that role,
which ``luciel_app`` deliberately does not have. That step lives in
``scripts/lock_audit_log.sql`` (post-migrate, superuser) and is what makes the lock a
lock. Each table is therefore only touched while the app role still owns it; once the
lock has run, ownership has moved and this migration is a no-op.

Revision ID: 0019_audit_truncate_guards
Revises: 0018_platform_sweep_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_audit_truncate_guards"
down_revision: str | None = "0018_platform_sweep_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

# (table, truncate-trigger name). Row-level guards keep the names their creators gave
# them (lock_audit_log.sql / migration 0012) so nothing is renamed out from under the
# ops runbook or the tamper tests that DISABLE them by name.
_TABLES = (
    ("admin_audit_log", "trg_audit_no_truncate"),
    ("internal_access_log", "trg_internal_access_log_no_truncate"),
)

# Created only if absent: once scripts/lock_audit_log.sql has run this function is
# owned by luciel_audit_owner and the app role must not (and cannot) replace it.
_GUARD_FN = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = '_audit_append_only_guard') THEN
    CREATE FUNCTION _audit_append_only_guard() RETURNS trigger
      LANGUAGE plpgsql AS $fn$
    BEGIN
      RAISE EXCEPTION
        '% is append-only (Architecture §5.3/§5.11): % is not permitted',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
    END;
    $fn$;
  END IF;
END $$;
"""


def _owned_by_current_user(table: str) -> str:
    # `table` is interpolated from the module-level _TABLES literal, never from input, and
    # identifiers cannot be bound as parameters in DDL — so interpolation is the only form.
    return f"""
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_roles r ON r.oid = c.relowner
     WHERE n.nspname = 'public' AND c.relname = '{table}' AND r.rolname = current_user
  ) THEN
    RAISE NOTICE '0019: {table} not owned by %, left to the superuser lock', current_user;
    RETURN;
  END IF;
"""  # noqa: S608  # nosec B608


def upgrade() -> None:
    op.execute(_GUARD_FN)
    for table, trigger in _TABLES:
        op.execute(
            f"""
DO $$
BEGIN
{_owned_by_current_user(table)}
  -- The owner's privileges are just a grant to itself, and migration 0001 revoked
  -- them down to SELECT+INSERT on admin_audit_log — which takes TRIGGER with it. So
  -- an owner is not necessarily able to install a trigger here, and re-granting
  -- TRIGGER to itself is the very self-grant the append-only doctrine forbids. Leave
  -- that table's guards to the superuser lock and install what we legitimately can.
  IF has_table_privilege(current_user, '{table}', 'TRIGGER') THEN
    DROP TRIGGER IF EXISTS {trigger} ON {table};
    CREATE TRIGGER {trigger}
      BEFORE TRUNCATE ON {table}
      FOR EACH STATEMENT EXECUTE FUNCTION _audit_append_only_guard();
  ELSE
    RAISE NOTICE
      '0019: % has no TRIGGER privilege on {table}; guard left to scripts/lock_audit_log.sql',
      current_user;
  END IF;

  -- TRUNCATE is a privilege of its own: this is what removes the D bit migration 0012
  -- left on internal_access_log, and it is the grant state that survives the
  -- ownership transfer the superuser lock performs.
  REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM luciel_app;
  GRANT SELECT, INSERT ON {table} TO luciel_app;
END $$;
"""
        )


def downgrade() -> None:
    for table, trigger in _TABLES:
        op.execute(
            f"""
DO $$
BEGIN
{_owned_by_current_user(table)}
  DROP TRIGGER IF EXISTS {trigger} ON {table};
END $$;
"""
        )
