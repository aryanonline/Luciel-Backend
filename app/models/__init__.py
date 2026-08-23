"""ORM model registry.

Importing this package imports every model module so that:
  - SQLAlchemy metadata is fully populated for Alembic autogenerate, and
  - the RLS policy generator (app/db/rls.py) sees every TenantMixin table.
"""

from app.models.audit import AdminAuditLog
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.booking import BookingEvent
from app.models.budget import ConversationBudget, CountedSession
from app.models.connection import InstanceConnection
from app.models.contact import ContactSubmission
from app.models.conversation import (
    EscalationEvent,
    Session,
    SessionSummary,
    Transcript,
)
from app.models.crm_link import CrmLeadLink
from app.models.deferred import DeferredAction
from app.models.instance import Account, Instance
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeSource,
)
from app.models.lead import Lead
from app.models.notice_delivery import NoticeDelivery
from app.models.record_source import RecordSourceRecord
from app.models.tool_auth import InstanceToolAuthorization
from app.models.trace import RetrievalTraceEvent, ToolTraceEvent
from app.models.voice import VoiceCallRecord

__all__ = [
    "ToolTraceEvent",
    "RetrievalTraceEvent",
    "InstanceToolAuthorization",
    "Account",
    "Instance",
    "Lead",
    "AdminAuditLog",
    "BillingAccount",
    "BookingEvent",
    "CrmLeadLink",
    "KnowledgeSource",
    "KnowledgeChunk",
    "KnowledgeEntity",
    "KnowledgeEdge",
    "NoticeDelivery",
    "RecordSourceRecord",
    "Session",
    "Transcript",
    "SessionSummary",
    "EscalationEvent",
    "InstanceConnection",
    "ContactSubmission",
    "ConversationBudget",
    "CountedSession",
    "ProcessedStripeEvent",
    "VoiceCallRecord",
    "DeferredAction",
]
