"""arc9 foundation: accounts, instances, leads, admin_audit_log + 3-layer isolation

Creates the foundational tenant tables and installs Layer-2 (RLS) + grants for
the three-layer tenant isolation pattern (Architecture §3.7.2b). The audit log is
created append-only (INSERT+SELECT grant only; FORCE RLS) per §5.3.

Revision ID: 0001_arc9_foundation
Revises:
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_arc9_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "luciel_app"

TENANT_TABLES = ["accounts", "instances", "leads", "admin_audit_log"]
APPEND_ONLY = {"admin_audit_log"}


def _install_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    # NULLIF(...,'') + missing_ok=true: when app.admin_id is unset/empty the cast
    # yields NULL, so the comparison is NULL → ZERO rows (safe default), rather than
    # raising on an empty-string uuid cast (Architecture §3.7.2b Layer 3).
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid) "
        f"WITH CHECK (admin_id = NULLIF(current_setting('app.admin_id', true), '')::uuid)"
    )
    # Grants: append-only tables get INSERT+SELECT only (no UPDATE/DELETE) — §5.3
    op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    if table in APPEND_ONLY:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    else:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    # Enums. create_type=False so the table-create below does NOT auto-emit them;
    # we create them explicitly once here (checkfirst guards re-runs).
    account_state = postgresql.ENUM(
        "unverified",
        "verified",
        "paused",
        "pending_deletion",
        "closed",
        name="account_state",
        create_type=False,
    )
    luciel_state = postgresql.ENUM(
        "active",
        "paused",
        "luciel_grace_window",
        "luciel_hard_deleted",
        name="luciel_state",
        create_type=False,
    )
    lead_state = postgresql.ENUM("active", "archived", name="lead_state", create_type=False)
    actor_type = postgresql.ENUM(
        "admin_user",
        "platform",
        "luciel_runtime",
        name="actor_type",
        create_type=False,
    )
    bind = op.get_bind()
    for e in (account_state, luciel_state, lead_state, actor_type):
        e.create(bind, checkfirst=True)

    # --- accounts ---
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_lower", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("state", account_state, nullable=False, server_default="unverified"),
        sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("has_completed_first_run", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("session_epoch", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pending_verify_jti", sa.String(64), nullable=True),
        sa.Column("pending_reset_jti", sa.String(64), nullable=True),
        sa.Column("resend_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("resend_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_accounts_admin_id", "accounts", ["admin_id"])
    op.create_index("ix_accounts_email_lower", "accounts", ["email_lower"])
    # Unique email only over verified accounts; unverified/re-signup can overwrite (§3.7.1a).
    op.execute(
        "CREATE UNIQUE INDEX uq_accounts_verified_email ON accounts (email_lower) "
        "WHERE state = 'verified'"
    )

    # --- instances ---
    op.create_table(
        "instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("website_url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("business_one_liner", sa.String(280), nullable=False, server_default=""),
        sa.Column("state", luciel_state, nullable=False, server_default="active"),
        sa.Column("embed_key_public_id", sa.String(64), nullable=True),
        sa.Column("grace_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_instances_admin_id", "instances", ["admin_id"])
    op.create_index("ix_instances_embed_key", "instances", ["embed_key_public_id"])

    # --- leads ---
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("contact_identifier", sa.String(512), nullable=True),
        sa.Column("intent", sa.String(2048), nullable=True),
        sa.Column("state", lead_state, nullable=False, server_default="active"),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_leads_admin_id", "leads", ["admin_id"])

    # --- admin_audit_log (append-only) ---
    op.create_table(
        "admin_audit_log",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", actor_type, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("prev_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_admin_id", "admin_audit_log", ["admin_id"])
    op.create_index("ix_audit_admin_created", "admin_audit_log", ["admin_id", "created_at"])

    for t in TENANT_TABLES:
        _install_rls(t)

    # Auth-time email lookup: a narrow SECURITY DEFINER function that resolves an
    # email to its (account_id) WITHOUT requiring app.admin_id to be set first.
    # This is the ONE pre-subject lookup the auth flow needs (signup/login/reset);
    # it returns only the account id (the future subject), never cross-tenant data,
    # and is owned by a role that can see the row. Everything after resolution runs
    # under the resolved tenant's app.admin_id (Architecture §3.7.2b Layer 3).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_account(p_email text)
        RETURNS TABLE(account_id uuid, password_hash text, state account_state,
                      email_verified boolean, session_epoch int)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT id, password_hash, state, email_verified, session_epoch
            FROM accounts WHERE email_lower = lower(p_email) LIMIT 1
        $$;
        """
    )
    # Owned by the narrow BYPASSRLS auth role so this ONE pre-subject lookup can
    # resolve an email to its account. Provisioning of luciel_auth is an infra
    # prerequisite (see scripts/bootstrap_roles.sql + docker-compose init); the
    # migration only re-owns the function (app role is a member of luciel_auth).
    op.execute("ALTER FUNCTION auth_lookup_account(text) OWNER TO luciel_auth")
    op.execute(f"GRANT EXECUTE ON FUNCTION auth_lookup_account(text) TO {APP_ROLE}")
    # The function runs as its owner (SECURITY DEFINER); the owner needs SELECT on
    # accounts and BYPASSRLS (luciel_auth has BYPASSRLS) so the one pre-subject
    # lookup can read the resolving row. Scope is the narrow function only.
    op.execute("GRANT SELECT ON accounts TO luciel_auth")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_account(text)")
    for t in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
    op.drop_table("admin_audit_log")
    op.drop_table("leads")
    op.drop_table("instances")
    op.execute("DROP INDEX IF EXISTS uq_accounts_verified_email")
    op.drop_table("accounts")
    for name in ("account_state", "luciel_state", "lead_state", "actor_type"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
