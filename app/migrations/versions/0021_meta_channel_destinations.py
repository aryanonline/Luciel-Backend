"""route inbound Meta traffic by PER-CHANNEL destination (§3.8.1)

One Meta grant authorizes WhatsApp, Instagram DMs and Messenger, so the single
``channel_auth`` connection now names one asset per channel under
``non_secret_config->'destinations'`` (``{"whatsapp": "<phone_number_id>",
"instagram": "<page_id>", "messenger": "<page_id>"}``) instead of a single
``destination``. Binding the second channel must not unbind the first.

``channel_resolve_destination`` is the pre-tenant SECURITY DEFINER lookup every inbound
webhook runs, and it only matched the scalar ``destination``. Left alone, an inbound
WhatsApp message to a per-channel-bound tenant resolves to nobody and is rejected. This
widens the match to "the scalar destination OR any value in the destinations map",
keeping the old shape working for connections bound before this revision.

Function body only — no table, column or data change.

Revision ID: 0021_meta_channel_destinations
Revises: 0020_lead_retention_rule
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_meta_channel_destinations"
down_revision: str | None = "0020_lead_retention_rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


_WITH_DESTINATIONS_MAP = """
    CREATE OR REPLACE FUNCTION channel_resolve_destination(p_ct text, p_dest text)
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
        LIMIT 1
    $$;
"""

_SCALAR_ONLY = """
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


def _redefine(body: str) -> None:
    op.execute(body)
    # CREATE OR REPLACE keeps the existing owner/grants, but restate them so the function
    # is correct even if it was dropped and recreated out of band.
    op.execute("ALTER FUNCTION channel_resolve_destination(text, text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION channel_resolve_destination(text, text) TO luciel_app")


def upgrade() -> None:
    _redefine(_WITH_DESTINATIONS_MAP)


def downgrade() -> None:
    _redefine(_SCALAR_ONLY)
