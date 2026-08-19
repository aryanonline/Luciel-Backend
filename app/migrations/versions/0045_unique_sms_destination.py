"""Partial unique index: one live claim per SMS/voice destination (c21 belt)

The application-level uniqueness gate (attach_sms_number → owner_of_destination,
c15) plus the per-destination advisory lock (c21) prevent duplicate claims at the
claim points; this index makes the invariant STRUCTURAL — a code path that forgets
the gate cannot write a second live claim for the same number, the database
refuses. Scope is deliberately narrow:

  * ``sms_sender`` scalar destinations only. Meta's per-channel ``destinations``
    JSONB map is not expressible as a unique index and stays gate-enforced;
    email_sender scalar destinations stay gate-enforced too (legacy platform
    rows keep a claim after disconnect BY DESIGN, service.py, and remain
    resolvable duplicates under 0044's newest-wins ordering).
  * Non-revoked rows only, matching exactly what ``channel_resolve_destination``
    routes on.

DATA PRECONDITION: no two non-revoked sms_sender rows may share a destination.
The one known legacy duplicate (+1236…, a stale test tenant vs the live claimant,
live-caught 2026-08-17) is released by ops BEFORE this deploys. The index create
is deliberately BARE — if a duplicate still exists the migration fails loudly,
because silently skipping the index would hide a tenant-crossing hazard behind a
successful-looking deploy (module CLAUDE.md review prompt).

Revision ID: 0045_unique_sms_destination
Revises: 0044_destination_resolver_order
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045_unique_sms_destination"
down_revision: str | None = "0044_destination_resolver_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_sms_destination ON instance_connections "
        "((non_secret_config->>'destination')) "
        "WHERE connection_type = 'sms_sender' AND status <> 'revoked' "
        "AND non_secret_config->>'destination' IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_sms_destination")
