"""ContactSubmission — the marketing-site contact form's durable record (ruling 1).

NOT tenant-scoped, deliberately. A contact submission arrives from an anonymous visitor
with no session and no ``admin_id``: there is no tenant to scope it to, and inventing one
would put a stranger's message inside a customer's RLS boundary. It sits alongside
``internal_access_log`` as the second platform-level table.

The row is the delivery guarantee, not the email. Email is best-effort — SES can be down,
the platform sender can be unconfigured, and in mock mode nothing leaves the process at
all — so the endpoint answers "accepted" on the strength of the INSERT and records
whether the notification actually went out. Answering on the email instead would either
drop messages silently or lie about having sent one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

#: Field ceilings, enforced at the schema layer so a rejection is a 422 with a reason
#: rather than a database error. Generous enough for a real enquiry, bounded enough that
#: the form is not a free blob store.
MAX_NAME = 200
MAX_EMAIL = 320  # RFC 5321 maximum addr-spec length
MAX_MESSAGE = 5000


class ContactSubmission(Base, TimestampMixin):
    """One marketing-site contact form submission."""

    __tablename__ = "contact_submission"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(MAX_NAME), nullable=False)
    email: Mapped[str] = mapped_column(String(MAX_EMAIL), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    #: Whether the platform notification email left the process. False in mock mode and
    #: whenever SES refused — the submission is still stored and still answerable.
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Coarse source attribution for abuse triage. Never surfaced to the submitter.
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
