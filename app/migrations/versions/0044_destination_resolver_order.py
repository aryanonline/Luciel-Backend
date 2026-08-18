"""channel_resolve_destination: newest non-revoked claim wins (deterministic)

The resolver picked an ARBITRARY row (``LIMIT 1`` with no ordering) when more than
one non-revoked connection claimed the same destination. Duplicates should never
exist — ``attach_sms_number`` now runs the same cross-tenant uniqueness gate as the
email and Meta destination binds — but rows claimed before that gate existed can
still collide (live-caught 2026-08-17: a stale test tenant's number claim captured
another tenant's inbound voice calls, refused honestly but wrongly).

For such legacy duplicates the NEWEST claim wins: the most recent tenant to walk
the designate flow passed a REAL Twilio verify against the number's account at
claim time, so they demonstrably controlled it most recently; an older claimant may
not control it at all any more. ``connection_id`` breaks created_at ties (same-
transaction timestamps are identical) so resolution — and therefore both the
signature-token lookup and tenant routing, which share this function — always
agrees with itself.

ORDER BY only — the match logic is byte-for-byte the 0021 body (scalar destination
OR the per-channel destinations map; 0037 was a data backfill, not a body change).
Replacing from an older body would silently drop the map matching and break
WhatsApp/Messenger/Instagram resolution — caught by the meta webhook contract
tests when a draft of this migration did exactly that.

Revision ID: 0044_destination_resolver_order
Revises: 0043_notice_deliveries
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044_destination_resolver_order"
down_revision: str | None = "0043_notice_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


_ORDERED = """
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
        ORDER BY created_at DESC, connection_id DESC
        LIMIT 1
    $$;
"""

_UNORDERED_0021 = """
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


def _restate_owner_and_grant() -> None:
    # CREATE OR REPLACE keeps the existing owner/grants, but restate them (0006
    # pattern) so this migration is self-sufficient on a fresh database too.
    op.execute("ALTER FUNCTION channel_resolve_destination(text, text) OWNER TO luciel_auth")
    op.execute("GRANT EXECUTE ON FUNCTION channel_resolve_destination(text, text) TO luciel_app")


def upgrade() -> None:
    op.execute(_ORDERED)
    _restate_owner_and_grant()


def downgrade() -> None:
    op.execute(_UNORDERED_0021)
    _restate_owner_and_grant()
