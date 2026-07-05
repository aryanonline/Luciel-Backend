"""add instance_connections.status value pending_email_routing (P0-2, §3.1.6a)

Architecture §3.1.6a / Decision #49: the tenant provisions the email address Luciel
uses. An own-domain address requires a walked-through inbound DNS/MX routing step, so
the email_sender connection shows a ``pending_email_routing`` status (NOT "connected")
until routing is verified — the same non-silent-precondition posture as SMS carrier
registration (``pending_carrier_registration``, migration 0013). Subdomain mode skips
DNS and is immediately ``connected``.

The ``instance_connections.status`` column is free-form VARCHAR(32) with no CHECK/enum
constraint (widened to 32 in migration 0013), and ``pending_email_routing`` is 21 chars,
so NO column change is required — this new value is stored as-is. The change is
therefore an ADDITIVE, wire/enum-level widening kept in lockstep across three layers:
the model/status vocabulary, the wire enum (``schemas_api.luciel.ConnectionStatusWire``),
and the frontend Zod ``connectionStatus`` enum (Luciel-Frontend PR #13). This migration
records that lockstep change on the chain; there are no existing rows to migrate.

Revision ID: 0015_conn_status_email_routing
Revises: 0014_sync_status_rename
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0015_conn_status_email_routing"
down_revision: str | None = "0015_deferred_resolved_lead"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # No-op at the DB layer: status is free-form VARCHAR(32) (no CHECK/enum), already
    # wide enough for `pending_email_routing`. The additive value lives in the model +
    # wire + frontend enums; this revision anchors that change on the migration chain.
    pass


def downgrade() -> None:
    # Nothing to reverse structurally; any pending_email_routing rows are resolved by
    # completing routing (→ connected) or revoking, not by a schema rollback.
    pass
