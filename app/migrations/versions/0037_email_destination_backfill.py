"""Backfill the inbound-routing destination for existing email_sender rows.

``channel_resolve_destination`` matches ``non_secret_config->>'destination'`` (or the
``destinations`` map) — but ``provision_email`` only ever wrote ``{address, mode}``,
so no email_sender row on any environment carries a destination and SES inbound email
resolves NO tenant at all: every inbound message bounced off "unknown destination"
while the dashboard showed a configured address. The code now writes ``destination``
at provision (and at mailbox verify); this backfills every row provisioned before.

Idempotent and additive: only rows WITH an address and WITHOUT a destination are
touched, and the destination is set to the row's own address (the exact value the
code would have written). Non-revoked and revoked rows alike are backfilled — the
resolver already excludes revoked rows at delivery time, and a restore should come
back routable.

Revision ID: 0037_email_destination_backfill
Revises: 0036_transcript_archive_columns
"""

from __future__ import annotations

from alembic import op

revision: str = "0037_email_destination_backfill"
down_revision: str | None = "0036_transcript_archive_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE instance_connections
        SET non_secret_config = non_secret_config
            || jsonb_build_object('destination', non_secret_config->>'address')
        WHERE connection_type = 'email_sender'
          AND COALESCE(non_secret_config->>'address', '') <> ''
          AND non_secret_config->>'destination' IS NULL
        """
    )


def downgrade() -> None:
    # Remove ONLY destinations that equal the address (i.e. ones this backfill wrote
    # or the code would have written identically) — a hand-bound different value is
    # not this migration's to delete.
    op.execute(
        """
        UPDATE instance_connections
        SET non_secret_config = non_secret_config - 'destination'
        WHERE connection_type = 'email_sender'
          AND non_secret_config->>'destination' = non_secret_config->>'address'
        """
    )
