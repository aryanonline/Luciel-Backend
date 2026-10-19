"""audit created_at index: the weekly audit_retention discovery stops scanning the log

Round 7 WP-8b (infra/cost truths). ``platform_sweep_candidates('audit_retention')``
(0072's body) finds the tenants with an audit row past the published 1-year retention
with ``SELECT DISTINCT l.admin_id FROM admin_audit_log l WHERE l.created_at < now() -
interval '365 days'``. Every index on the table leads with ``admin_id`` (0001's
``ix_audit_admin_id`` / ``ix_audit_admin_created``, 0003's ``ix_audit_admin_seq``), so
that predicate is a full sequential scan of the one table that only ever grows — once
a week, as the sweep's whole discovery step. ``ix_audit_created_at`` turns it into a
range scan over the aged rows alone; the per-tenant DELETE inside
``audit_retention_prune`` keeps using ``ix_audit_admin_created``.

THE AUDIT-LOCK CONSIDERATION (why this migration is conditional). ``CREATE INDEX``
needs the table's OWNER, and after ``scripts/lock_audit_log.sql`` has run the owner is
``luciel_audit_owner`` — a role ``luciel_app`` (the role alembic runs as) is
deliberately not a member of, because that non-membership IS the append-only lock
(§5.3). So:

* on a database whose log is not locked yet (a fresh environment, CI, the test DB —
  alembic runs before the lock step there) this migration builds the index as the
  table's owner, and the lock then hands it to ``luciel_audit_owner`` with the table;
* on an already-locked database (every deployed environment) the app role cannot
  build it, so this migration leaves a NOTICE and ``scripts/lock_audit_log.sql`` —
  step 3 of ``scripts/migrate.sh``, run as the master role after every
  ``alembic upgrade head`` — builds it with ``CREATE INDEX IF NOT EXISTS``. The lock
  step is the documented home of every audit-table DDL the app role may not perform.

Plain ``CREATE INDEX`` (not ``CONCURRENTLY``): alembic runs each migration inside a
transaction, and so does the lock script (it is executed as one multi-statement
script), and ``CONCURRENTLY`` refuses to run in a transaction block. The plain build
takes a SHARE lock on ``admin_audit_log`` for its duration: audit INSERTs — every
audited action — wait behind it, SELECTs do not. The table is small today (dev holds
thousands of rows; the build is sub-second), the migration runs before the services
roll (deploy.yml), and the wait is bounded by the build; if the log ever grows to
where that matters, build it by hand first with ``CREATE INDEX CONCURRENTLY IF NOT
EXISTS ix_audit_created_at ON admin_audit_log (created_at)`` as the master role and
both paths below become no-ops.

No row is touched, so no ``luciel_auth`` step is needed. Downgrade drops the index
only where the app role owns the table; on a locked database the index belongs to the
lock step and stays (harmless — nothing depends on its absence).

Revision ID: 0075_audit_created_at_index
Revises: 0074_session_transport_sender
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0075_audit_created_at_index"
down_revision: str | None = "0074_session_transport_sender"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

INDEX_NAME = "ix_audit_created_at"

# Owner test: pg_has_role(..., 'USAGE') is true for the owner itself and for any role
# that inherits it — exactly the set CREATE INDEX / DROP INDEX accepts.
_OWNS_THE_LOG = (
    "pg_has_role(current_user, "
    "(SELECT relowner FROM pg_class WHERE relname = 'admin_audit_log' "
    "AND relnamespace = 'public'::regnamespace), 'USAGE')"
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF {_OWNS_THE_LOG} THEN
            CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON admin_audit_log (created_at);
          ELSE
            RAISE NOTICE 'admin_audit_log is owner-locked (luciel_audit_owner): %',
              '{INDEX_NAME} is built by scripts/lock_audit_log.sql (migrate.sh step 3)';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF {_OWNS_THE_LOG} THEN
            DROP INDEX IF EXISTS {INDEX_NAME};
          ELSE
            RAISE NOTICE 'admin_audit_log is owner-locked: % stays (the lock step owns it)',
              '{INDEX_NAME}';
          END IF;
        END $$;
        """
    )
