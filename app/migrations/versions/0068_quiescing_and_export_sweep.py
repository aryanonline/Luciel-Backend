"""quiescing connections drain on every channel; accounts remember when they closed

Round 7 WP-5 (lifecycle, export and closure truth). Three persistence changes:

1. ``channel_resolve_destination`` gains ``p_include_quiescing boolean DEFAULT true``.
   A Luciel delete now writes ``status = 'quiescing'`` on every connection instead of
   ``revoked`` (app/lifecycle/state_machine.py). The old body admitted every
   non-revoked row, so a quiescing row already routed — but nothing SAID so, and the
   claim gate (``routing.owner_of_destination``) shares this function, so admitting
   the row for routing would also have made it HOLD its destination for the whole
   grace window, a product change c21 never intended. The flag splits the two
   callers on purpose: inbound routing passes ``true`` (the customer mid-conversation
   at the delete instant keeps reaching the tenant — the §3.6.3 drain on SMS / email /
   WhatsApp / Messenger / Instagram, not only the widget), the claim gate passes
   ``false`` (a draining row releases its number/address exactly as a revoked one
   did, and the newest live claim wins from that moment on). Return type and the
   0044 newest-claim-wins ordering are unchanged; the match logic is byte-for-byte
   the 0021/0044 body (scalar destination OR the per-channel destinations map).

   ``CREATE OR REPLACE`` cannot change a function's argument list, so the 2-arg
   function is dropped and the 3-arg one created; nothing depends on it in the
   catalog (the app calls it from ``routing.py`` only). OWNER + GRANT are restated
   (0006/0044 pattern) — a SECURITY DEFINER replace that dropped the ACL would break
   every tenant's inbound at once.

2. ``uq_sms_destination`` (0045) is recreated to exclude ``quiescing`` rows as well as
   ``revoked`` ones, so a legitimate claim by another tenant during a deleted
   Luciel's grace window is structurally possible again (the claim gate says
   "unclaimed", the index must agree). ``uq_email_destination`` (0067, the email
   claim index written in parallel with this migration) is recreated the same way:
   chained after 0067 at merge, it would otherwise keep counting a draining mailbox
   as a live claim and answer a legitimate reclaim with a unique violation.

3. ``accounts.closed_at`` — when the owner closed the account (§3.6.6). The purge sweep
   used to read the FIRST ``account_closure_initiated`` audit row as its clock; with
   owner decision D5 (2026-09-14: a closed account can be REOPENED for 30 days) an
   account can close more than once, and the first-ever closure would purge a
   re-closed account on the spot. The column is stamped at close, cleared at reopen;
   ``close_purge`` prefers it and falls back to the audit chain only for rows closed
   before this migration (NULL).

``uq_conn_active_type`` (0002/0058) is deliberately NOT touched: a quiescing row keeps
its type's single-active slot, so a re-attach during grace reuses the draining row
(``connections.service`` reads any non-revoked row) instead of sitting beside it —
every non-revoked reader in the senders and the connection service is single-row.

Downgrade returns every quiescing row to ``revoked`` (the pre-WP-5 delete
semantics — the index precondition needs it, and a quiescing row means nothing to
the older code), restores the 0045 index predicate and the 0044 function body, and
drops the column. The row flip runs as ``luciel_auth`` (BYPASSRLS) for the statement:
every tenant table is FORCE RLS and alembic runs as ``luciel_app`` with no tenant
pinned, under which a cross-tenant UPDATE matches nothing. Migration time is the
one window the app role is a member of ``luciel_auth`` (bootstrap grants it, the
lock script revokes it) — the same membership the OWNER TO statements above rely on.

Revision ID: 0068_quiescing_and_export_sweep
Revises: 0065_embedding_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_quiescing_and_export_sweep"
down_revision: str | None = "0067_lead_claims_and_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


_RESOLVER_0068 = """
    CREATE FUNCTION channel_resolve_destination(
        p_ct text, p_dest text, p_include_quiescing boolean DEFAULT true
    )
    RETURNS TABLE(admin_id uuid, instance_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT admin_id, instance_id FROM instance_connections
        WHERE connection_type = p_ct
          AND status <> 'revoked'
          -- 'quiescing' = a Luciel in its delete drain (round 7 WP-5): routable for
          -- inbound so the in-flight conversation finishes on its channel, but NOT
          -- a destination holder for the claim gate.
          AND (p_include_quiescing OR status <> 'quiescing')
          AND (
            non_secret_config->>'destination' = p_dest
            OR EXISTS (
              SELECT 1
              FROM jsonb_each_text(
                COALESCE(non_secret_config->'destinations', '{}'::jsonb)
              ) AS d(channel, value)
              WHERE d.value = p_dest
            )
          )
        ORDER BY created_at DESC, connection_id DESC
        LIMIT 1
    $$;
"""

# 0044's ORDERED body, verbatim (the 2-arg shape the downgrade restores).
_RESOLVER_0044 = """
    CREATE FUNCTION channel_resolve_destination(p_ct text, p_dest text)
    RETURNS TABLE(admin_id uuid, instance_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT admin_id, instance_id FROM instance_connections
        WHERE connection_type = p_ct
          AND status <> 'revoked'
          AND (
            non_secret_config->>'destination' = p_dest
            OR EXISTS (
              SELECT 1
              FROM jsonb_each_text(
                COALESCE(non_secret_config->'destinations', '{}'::jsonb)
              ) AS d(channel, value)
              WHERE d.value = p_dest
            )
          )
        ORDER BY created_at DESC, connection_id DESC
        LIMIT 1
    $$;
"""

_INDEX_0068 = (
    "CREATE UNIQUE INDEX uq_sms_destination ON instance_connections "
    "((non_secret_config->>'destination')) "
    "WHERE connection_type = 'sms_sender' AND status NOT IN ('revoked', 'quiescing') "
    "AND non_secret_config->>'destination' IS NOT NULL"
)

_EMAIL_INDEX_0068 = (
    "CREATE UNIQUE INDEX uq_email_destination ON instance_connections "
    "((lower(non_secret_config->>'destination'))) "
    "WHERE connection_type = 'email_sender' AND status NOT IN ('revoked', 'quiescing') "
    "AND non_secret_config->>'destination' IS NOT NULL"
)

_EMAIL_INDEX_0067 = (
    "CREATE UNIQUE INDEX uq_email_destination ON instance_connections "
    "((lower(non_secret_config->>'destination'))) "
    "WHERE connection_type = 'email_sender' AND status <> 'revoked' "
    "AND non_secret_config->>'destination' IS NOT NULL"
)

_INDEX_0045 = (
    "CREATE UNIQUE INDEX uq_sms_destination ON instance_connections "
    "((non_secret_config->>'destination')) "
    "WHERE connection_type = 'sms_sender' AND status <> 'revoked' "
    "AND non_secret_config->>'destination' IS NOT NULL"
)


def _restate_owner_and_grant(signature: str) -> None:
    op.execute(f"ALTER FUNCTION channel_resolve_destination({signature}) OWNER TO luciel_auth")
    op.execute(f"GRANT EXECUTE ON FUNCTION channel_resolve_destination({signature}) TO luciel_app")


def upgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS channel_resolve_destination(text, text)")
    op.execute(_RESOLVER_0068)
    _restate_owner_and_grant("text, text, boolean")

    op.execute("DROP INDEX IF EXISTS uq_sms_destination")
    op.execute(_INDEX_0068)
    op.execute("DROP INDEX IF EXISTS uq_email_destination")
    op.execute(_EMAIL_INDEX_0068)

    op.add_column("accounts", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "closed_at")

    # The older code has no notion of a draining row: a quiescing connection is what a
    # pre-WP-5 delete would have written as revoked. Flipping them first also satisfies
    # the 0045 index's precondition (no two non-revoked sms rows share a destination).
    # As luciel_auth, for this statement only: instance_connections is FORCE RLS and
    # the app role sees no rows without a tenant pinned (see the module docstring).
    op.execute("GRANT UPDATE ON instance_connections TO luciel_auth")
    op.execute("SET LOCAL ROLE luciel_auth")
    op.execute("UPDATE instance_connections SET status = 'revoked' WHERE status = 'quiescing'")
    op.execute("RESET ROLE")
    op.execute("REVOKE UPDATE ON instance_connections FROM luciel_auth")
    op.execute("DROP INDEX IF EXISTS uq_sms_destination")
    op.execute(_INDEX_0045)
    op.execute("DROP INDEX IF EXISTS uq_email_destination")
    op.execute(_EMAIL_INDEX_0067)

    op.execute("DROP FUNCTION IF EXISTS channel_resolve_destination(text, text, boolean)")
    op.execute(_RESOLVER_0044)
    _restate_owner_and_grant("text, text")
