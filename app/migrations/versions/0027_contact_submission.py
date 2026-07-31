"""contact_submission: give the marketing contact form somewhere to land (ruling 1)

The frontend has shipped a contact form wired to ``POST /api/v1/contact`` and nothing
served it, so every enquiry a prospect typed was lost on submit. Ruling 1 says build it
real: captcha-verified, rate-limited, PERSISTED, and only then answered "accepted".

Not tenant-scoped and therefore no RLS: the submitter is an anonymous visitor with no
``admin_id``, and scoping a stranger's message into some tenant's isolation boundary
would be worse than not scoping it at all. It joins ``internal_access_log`` as a
platform-level table. The app role gets INSERT and SELECT only — the row is a record of
something a person said, so nothing on the request path may rewrite or erase it.

Revision ID: 0027_contact_submission
Revises: 0026_unverified_reap_candidates
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_contact_submission"
down_revision: str | None = "0026_unverified_reap_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "contact_submission",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        # Whether the platform notification actually left the process. False in mock
        # mode and on an SES refusal; the submission is stored and answerable either way.
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_ip", sa.String(45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Triage by arrival order; also the index an abuse review would read.
    op.create_index("ix_contact_submission_created_at", "contact_submission", ["created_at"])

    op.execute("GRANT INSERT, SELECT ON contact_submission TO luciel_app;")
    op.execute("REVOKE UPDATE, DELETE ON contact_submission FROM luciel_app;")


def downgrade() -> None:
    op.drop_index("ix_contact_submission_created_at", table_name="contact_submission")
    op.drop_table("contact_submission")
