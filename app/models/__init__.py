"""ORM model registry.

Importing this package imports every model module so that:
  - SQLAlchemy metadata is fully populated for Alembic autogenerate, and
  - the RLS policy generator (app/db/rls.py) sees every TenantMixin table.
"""

from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import (
    EscalationEvent,
    Session,
    SessionSummary,
    Transcript,
)
from app.models.instance import Account, Instance
from app.models.knowledge import KnowledgeChunk, KnowledgeSource
from app.models.lead import Lead
from app.models.tool_auth import InstanceToolAuthorization

__all__ = [
    "InstanceToolAuthorization",
    "Account",
    "Instance",
    "Lead",
    "AdminAuditLog",
    "KnowledgeSource",
    "KnowledgeChunk",
    "Session",
    "Transcript",
    "SessionSummary",
    "EscalationEvent",
    "InstanceConnection",
]
