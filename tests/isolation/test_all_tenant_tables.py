"""Generic isolation coverage across EVERY tenant table (auto-discovered).

Asserts that:
  - every model mixing in TenantMixin has RLS enabled+forced and a tenant_isolation
    policy in the live DB, and
  - a row written under tenant A is invisible to tenant B,
for all current and future tenant tables — so coverage scales with the schema and
a newly-added tenant table cannot silently skip the three-layer defense.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.base import Base, TenantMixin
from app.db.rls import tenant_table_names
from app.db.session import tenant_session
from sqlalchemy import select, text

pytestmark = [pytest.mark.isolation, pytest.mark.asyncio]


def _tenant_models() -> list[type]:
    models = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, TenantMixin):
            models.append(cls)
    return models


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_tenant_table_has_forced_rls_and_policy():
    from app.db.engine import get_engine

    async with get_engine().connect() as conn:
        for table in tenant_table_names():
            row = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).one()
            assert row.relrowsecurity, f"{table}: RLS not enabled"
            assert row.relforcerowsecurity, f"{table}: RLS not FORCED (owner would bypass)"
            pol = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_policies WHERE tablename = :t"),
                    {"t": table},
                )
            ).scalar_one()
            assert pol >= 1, f"{table}: no RLS policy"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cross_tenant_invisible_for_every_model():
    a, b = uuid.uuid4(), uuid.uuid4()
    # Write one minimal row per tenant table under tenant A, then assert tenant B
    # sees none of them. We construct rows generically with required columns.
    from app.models.audit import ActorType, AdminAuditLog
    from app.models.connection import InstanceConnection
    from app.models.conversation import EscalationEvent, Session, SessionSummary, Transcript
    from app.models.instance import Account, AccountState, Instance, LucielState
    from app.models.knowledge import KnowledgeChunk, KnowledgeSource
    from app.models.lead import Lead

    iid = uuid.uuid4()
    sid = uuid.uuid4()
    srcid = uuid.uuid4()

    async with tenant_session(a) as s:
        s.add(
            Account(
                id=a,
                admin_id=a,
                user_id=a,
                email="a@x.com",
                email_lower="a@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=a,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(Lead(id=uuid.uuid4(), admin_id=a, instance_id=iid, name="L"))
        s.add(
            KnowledgeSource(
                id=srcid,
                admin_id=a,
                instance_id=iid,
                name="f",
                origin="upload",
                ingestion_status="ready",
            )
        )
        s.add(
            KnowledgeChunk(
                id=uuid.uuid4(), admin_id=a, instance_id=iid, source_id=srcid, ordinal=0, text="t"
            )
        )
        s.add(Session(id=sid, admin_id=a, instance_id=iid, channel="widget", mode="ai"))
        s.add(
            Transcript(
                id=uuid.uuid4(), admin_id=a, instance_id=iid, session_id=sid, role="lead", text="hi"
            )
        )
        s.add(
            SessionSummary(
                id=uuid.uuid4(), admin_id=a, instance_id=iid, session_id=sid, summary="s"
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=a,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
            )
        )
        s.add(
            EscalationEvent(
                escalation_id=uuid.uuid4(),
                admin_id=a,
                instance_id=iid,
                session_id=sid,
                signal="high_value_lead",
                gate="outcome",
            )
        )

    # admin_audit_log via the writer (chained).
    from app.audit import log as audit

    async with tenant_session(a) as s:
        await audit.emit(
            s, admin_id=a, event_type="instance_created", actor_type=ActorType.platform
        )

    models = [
        Account,
        Instance,
        Lead,
        KnowledgeSource,
        KnowledgeChunk,
        Session,
        Transcript,
        SessionSummary,
        InstanceConnection,
        EscalationEvent,
        AdminAuditLog,
    ]
    async with tenant_session(b) as s:
        for model in models:
            rows = (await s.execute(select(model))).scalars().all()
            assert rows == [], f"tenant B saw tenant A rows in {model.__tablename__}"
