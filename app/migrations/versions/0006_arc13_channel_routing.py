"""arc13 channels: inbound-routing resolver functions (pre-subject lookups)

Inbound routing resolves a destination identifier → (admin_id, instance_id) BEFORE
a tenant subject exists (the webhook is authenticated by signature, not a JWT).
Like auth_lookup_account, these are narrow SECURITY DEFINER functions owned by the
BYPASSRLS luciel_auth role, returning only the resolving tenant ids.

  channel_resolve_embed_key(embed_key) -> (admin_id, instance_id)
  channel_resolve_destination(connection_type, destination) -> (admin_id, instance_id)

The destination match is against instance_connections.non_secret_config->>'destination'
for non-revoked rows (e.g. the provisioned Twilio number, the verified SES sender).

Revision ID: 0006_arc13_channel_routing
Revises: 0005_arc12_tool_authorizations
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_arc13_channel_routing"
down_revision: str | None = "0005_arc12_tool_authorizations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION channel_resolve_embed_key(p_key text)
        RETURNS TABLE(admin_id uuid, instance_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT admin_id, id FROM instances
            WHERE embed_key_public_id = p_key
              AND state IN ('active')
            LIMIT 1
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION channel_resolve_destination(p_ct text, p_dest text)
        RETURNS TABLE(admin_id uuid, instance_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT admin_id, instance_id FROM instance_connections
            WHERE connection_type = p_ct
              AND status <> 'revoked'
              AND non_secret_config->>'destination' = p_dest
            LIMIT 1
        $$;
        """
    )
    op.execute("ALTER FUNCTION channel_resolve_embed_key(text) OWNER TO luciel_auth")
    op.execute("ALTER FUNCTION channel_resolve_destination(text, text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION channel_resolve_embed_key(text) TO luciel_app")
    op.execute("GRANT EXECUTE ON FUNCTION channel_resolve_destination(text, text) TO luciel_app")
    # The functions run as luciel_auth (BYPASSRLS); grant it SELECT on the tables.
    op.execute("GRANT SELECT ON instances TO luciel_auth")
    op.execute("GRANT SELECT ON instance_connections TO luciel_auth")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS channel_resolve_embed_key(text)")
    op.execute("DROP FUNCTION IF EXISTS channel_resolve_destination(text, text)")
