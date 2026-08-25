"""crm_lead_links.lead_id — the link keys on the LEAD, not its email (round 5A #9)

The link table's documented promise — "a subsequent push PATCHes the exact record
the first push created, whatever the lead's identifying fields say now" — was
unimplemented: rows keyed on ``lead_key`` (the email) alone, so the moment a
lead's key changed the lookup missed and a duplicate CRM record appeared. This
adds ``lead_id``, backfills it where an unambiguous (admin, instance, key) →
Lead match exists, and swaps the unique constraint to the real invariant: one
CRM record per LEAD per provider. Ambiguous/orphan rows stay NULL and are
adopted lazily at push time by a key-ownership-verified resolution — never
guessed here.

The old unique-on-key constraint is dropped (lead_key is mutable now); a plain
index keeps the legacy-fallback lookup fast.

Revision ID: 0053_crm_link_lead_id
Revises: 0052_lead_email
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053_crm_link_lead_id"
down_revision: str | None = "0052_lead_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "crm_lead_links",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Backfill only UNAMBIGUOUS matches (count = 1); everything else is adopted
    # lazily at push time under the key-ownership guard. Cross-tenant UPDATE in a
    # migration is fine — migrations run superuser (precedent: 0032).
    op.execute(
        """
        UPDATE crm_lead_links cl
           SET lead_id = (SELECT l.id FROM leads l
                           WHERE l.admin_id = cl.admin_id
                             AND l.instance_id = cl.instance_id
                             AND (l.contact_identifier = cl.lead_key
                                  OR lower(l.contact_identifier) = lower(cl.lead_key)
                                  OR l.email = lower(cl.lead_key)))
         WHERE cl.lead_id IS NULL
           AND (SELECT count(*) FROM leads l
                 WHERE l.admin_id = cl.admin_id
                   AND l.instance_id = cl.instance_id
                   AND (l.contact_identifier = cl.lead_key
                        OR lower(l.contact_identifier) = lower(cl.lead_key)
                        OR l.email = lower(cl.lead_key))) = 1
        """
    )
    op.drop_constraint("uq_crm_lead_link", "crm_lead_links", type_="unique")
    op.create_index(
        "ix_crm_lead_links_key",
        "crm_lead_links",
        ["admin_id", "instance_id", "provider", "lead_key"],
    )
    op.create_index(
        "uq_crm_lead_link_lead",
        "crm_lead_links",
        ["admin_id", "instance_id", "provider", "lead_id"],
        unique=True,
        postgresql_where=sa.text("lead_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_crm_lead_link_lead", table_name="crm_lead_links")
    op.drop_index("ix_crm_lead_links_key", table_name="crm_lead_links")
    op.create_unique_constraint(
        "uq_crm_lead_link", "crm_lead_links", ["admin_id", "instance_id", "provider", "lead_key"]
    )
    op.drop_column("crm_lead_links", "lead_id")
