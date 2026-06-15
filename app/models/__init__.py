"""ORM model registry.

Importing this package imports every model module so that:
  - SQLAlchemy metadata is fully populated for Alembic autogenerate, and
  - the RLS policy generator (app/db/rls.py) sees every TenantMixin table.
"""

from app.models.audit import AdminAuditLog
from app.models.instance import Account, Instance
from app.models.lead import Lead

__all__ = ["Account", "Instance", "Lead", "AdminAuditLog"]
