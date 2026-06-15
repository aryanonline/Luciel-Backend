"""ORM model registry.

Importing this package imports every model module so that:
  - SQLAlchemy metadata is fully populated for Alembic autogenerate, and
  - the RLS policy generator (app/db/rls.py) sees every TenantMixin table.
"""

from app.models.audit import AdminAuditLog
from app.models.budget import ConversationBudget
from app.models.connection import InstanceConnection
from app.models.conversation import (
    EscalationEvent,
    Session,
    SessionSummary,
    Transcript,
)
from app.models.deferred import DeferredAction
from app.models.instance import Account, Instance
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeSource,
)
from app.models.lead import Lead
from app.models.tool_auth import InstanceToolAuthorization
from app.models.voice import VoiceCallRecord

__all__ = [
    "InstanceToolAuthorization",
    "Account",
    "Instance",
    "Lead",
    "AdminAuditLog",
    "KnowledgeSource",
    "KnowledgeChunk",
    "KnowledgeEntity",
    "KnowledgeEdge",
    "Session",
    "Transcript",
    "SessionSummary",
    "EscalationEvent",
    "InstanceConnection",
    "ConversationBudget",
    "VoiceCallRecord",
    "DeferredAction",
]
