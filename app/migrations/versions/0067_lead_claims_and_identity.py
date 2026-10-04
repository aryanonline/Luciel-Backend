"""lead claims and identity: one asset, one account (round 7 WP-3c items 10, 13, 14)

Owner rulings 2026-09-14 made structural:

  * Email claims are case-insensitive (item 10). ``verify.py`` already stored the
    Graph mailbox lower-case; the SES inbound recipient and ``routing.
    owner_of_destination`` now case-fold too. This migration rewrites the existing
    ``email_sender`` destinations to lower case ONCE (the byte-exact resolver from
    0044 then meets a single spelling) and adds the partial unique index
    ``uq_email_destination`` on ``lower(non_secret_config->>'destination')`` for
    non-revoked ``email_sender`` rows — the mirror of ``0045_unique_sms_destination``.
    DATA PRECONDITION, fail-loud exactly like 0045: if two non-revoked rows still
    claim one mailbox (case-folded) the migration REFUSES with the count; the
    duplicate sweep runs on the environment before this deploys. Silently skipping
    the index would hide a tenant-crossing hazard behind a successful deploy.

  * A shared CRM portal across two accounts is ALLOWED with an informational warning
    (item 13). RLS hides the other account's row from an ordinary query, so
    ``connection_config_holders`` is the narrow SECURITY DEFINER lookup (the 0044
    resolver's pattern) that answers "which accounts hold this hubId / orgId"; the
    OAuth callback uses it to add a note and an audit event, never a refusal.

  * ``instances.embed_key_public_id`` is UNIQUE (item 14): ``ix_instances_embed_key``
    becomes a unique index, and the mint sites retry on conflict. Same fail-loud
    precondition (a duplicate key would already be a routing ambiguity).

RLS AND THIS MIGRATION. Alembic connects as ``luciel_app`` — NOBYPASSRLS, every
tenant table FORCE ROW LEVEL SECURITY, no ``app.admin_id`` set — so a plain SELECT
or UPDATE here matches ZERO rows and "succeeds" having done nothing (the first
draft of this migration lower-cased nothing and counted no duplicates). So:

  * cross-tenant READS (the duplicate counts, which tenants hold a mixed-case
    destination) run under ``SET LOCAL ROLE luciel_auth`` — the narrow BYPASSRLS
    role, which already has SELECT on both tables (0006) and which the app role is
    a member of only for the migration window (``bootstrap_roles.sql`` grants,
    ``lock_audit_log.sql`` revokes; 2026-09-05 audit F085);
  * the WRITE runs tenant by tenant under that tenant's own ``app.admin_id``, as
    ``luciel_app``, so the policy's WITH CHECK still governs every row it touches
    and the BYPASSRLS role is never granted UPDATE, not even transiently.

Both index creates stay bare (index builds ignore RLS), so even with the counts
skipped the database itself would refuse duplicate data — the counts exist to
refuse BEFORE anything is rewritten and to say how many.

Revision ID: 0067_lead_claims_and_identity
Revises: 0065_embedding_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_lead_claims_and_identity"
down_revision: str | None = "0066_takeover_delivery_truth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


_COUNT_DUPLICATE_EMAIL_CLAIMS = """
    SELECT count(*) FROM (
        SELECT lower(non_secret_config->>'destination') AS mailbox
          FROM instance_connections
         WHERE connection_type = 'email_sender'
           AND status <> 'revoked'
           AND non_secret_config->>'destination' IS NOT NULL
         GROUP BY 1
        HAVING count(*) > 1
    ) dups
"""

_COUNT_DUPLICATE_EMBED_KEYS = """
    SELECT count(*) FROM (
        SELECT embed_key_public_id
          FROM instances
         WHERE embed_key_public_id IS NOT NULL
         GROUP BY 1
        HAVING count(*) > 1
    ) dups
"""

# Revoked rows are rewritten too: a restore must come back in the one spelling the
# resolver and the index know (0037 backfilled revoked rows for the same reason).
_TENANTS_WITH_MIXED_CASE_DESTINATIONS = """
    SELECT DISTINCT admin_id
      FROM instance_connections
     WHERE connection_type = 'email_sender'
       AND non_secret_config->>'destination' IS NOT NULL
       AND non_secret_config->>'destination' <> lower(non_secret_config->>'destination')
"""

_LOWERCASE_EMAIL_DESTINATIONS = """
    UPDATE instance_connections
       SET non_secret_config = jsonb_set(
               non_secret_config,
               '{destination}',
               to_jsonb(lower(non_secret_config->>'destination'))
           )
     WHERE connection_type = 'email_sender'
       AND non_secret_config->>'destination' IS NOT NULL
       AND non_secret_config->>'destination' <> lower(non_secret_config->>'destination')
"""

_CREATE_EMAIL_INDEX = """
    CREATE UNIQUE INDEX uq_email_destination ON instance_connections
        ((lower(non_secret_config->>'destination')))
        WHERE connection_type = 'email_sender' AND status <> 'revoked'
          AND non_secret_config->>'destination' IS NOT NULL
"""

_CONFIG_HOLDERS_FUNCTION = """
    CREATE OR REPLACE FUNCTION connection_config_holders(p_ct text, p_key text, p_value text)
    RETURNS TABLE(admin_id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
        SELECT DISTINCT c.admin_id
          FROM instance_connections c
         WHERE c.connection_type = p_ct
           AND c.status <> 'revoked'
           AND c.non_secret_config->>p_key = p_value
    $$;
"""


def upgrade() -> None:
    conn = op.get_bind()

    # --- fail-loud preconditions, read across tenants (see the module docstring) --- #
    conn.execute(sa.text("SET LOCAL ROLE luciel_auth"))
    email_dups = int(conn.execute(sa.text(_COUNT_DUPLICATE_EMAIL_CLAIMS)).scalar_one())
    embed_dups = int(conn.execute(sa.text(_COUNT_DUPLICATE_EMBED_KEYS)).scalar_one())
    mixed_case_tenants = [
        row[0] for row in conn.execute(sa.text(_TENANTS_WITH_MIXED_CASE_DESTINATIONS))
    ]
    conn.execute(sa.text("RESET ROLE"))
    if email_dups:
        raise RuntimeError(
            f"{revision} refused: {email_dups} email_sender destination(s) are claimed "
            "by more than one non-revoked row (case-folded). Release the duplicates "
            "before deploying; the index is never skipped."
        )
    if embed_dups:
        raise RuntimeError(
            f"{revision} refused: {embed_dups} embed key(s) are held by more than one "
            "instance. Resolve them before deploying; the index is never skipped."
        )

    # --- item 10: email claims are case-insensitive ------------------------------ #
    # One tenant at a time, under that tenant's own GUC, so RLS governs the write.
    for admin_id in mixed_case_tenants:
        conn.execute(
            sa.text("SELECT set_config('app.admin_id', :aid, true)"), {"aid": str(admin_id)}
        )
        conn.execute(sa.text(_LOWERCASE_EMAIL_DESTINATIONS))
    # Back to "no tenant": later migrations may share this transaction.
    conn.execute(sa.text("SELECT set_config('app.admin_id', '', true)"))
    op.execute(_CREATE_EMAIL_INDEX)

    # --- item 13: who else holds this CRM portal (informational, cross-tenant) ---- #
    op.execute(_CONFIG_HOLDERS_FUNCTION)
    op.execute("ALTER FUNCTION connection_config_holders(text, text, text) OWNER TO luciel_auth")
    op.execute(
        "GRANT EXECUTE ON FUNCTION connection_config_holders(text, text, text) TO luciel_app"
    )

    # --- item 14: one embed key, one instance ------------------------------------- #
    op.drop_index("ix_instances_embed_key", table_name="instances")
    op.create_index("ix_instances_embed_key", "instances", ["embed_key_public_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_instances_embed_key", table_name="instances")
    op.create_index("ix_instances_embed_key", "instances", ["embed_key_public_id"])
    op.execute("DROP FUNCTION IF EXISTS connection_config_holders(text, text, text)")
    op.execute("DROP INDEX IF EXISTS uq_email_destination")
    # The lower-casing is a one-way data normalization: the original spellings are
    # not kept, and every producer writes lower case from here on.
