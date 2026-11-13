"""Regression tests for Axis11-F02: internal_access_log model + write API.

Architecture §5.11 / Commitment C23: the internal access log is a concrete,
append-only artifact separate from admin_audit_log that records platform staff
access events (break_glass / support_scoped).

Tests verify:
  - emit_internal_access() persists a row to internal_access_log.
  - The row is separate from admin_audit_log (no cross-contamination).
  - Invalid access_type raises ValueError.
  - break_glass and support_scoped are accepted.
  - Rows are read back correctly (all fields preserved).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.audit.internal_access import (
    ACCESS_TYPE_BREAK_GLASS,
    ACCESS_TYPE_SUPPORT_SCOPED,
    emit_internal_access,
)
from app.db.session import untenanted_session
from app.models.audit import AdminAuditLog
from app.models.internal_access import InternalAccessLog
from sqlalchemy import select


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_emit_internal_access_persists_row():
    """Axis11-F02: emit_internal_access must write a row to internal_access_log."""
    admin = uuid.uuid4()
    now = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)

    async with untenanted_session() as s:
        entry = await emit_internal_access(
            s,
            staff_id="ops-eng@example.com",
            approver_id="lead-eng@example.com",
            admin_id=admin,
            data_class="leads",
            access_started_at=now,
            access_type=ACCESS_TYPE_BREAK_GLASS,
            justification="Customer-reported data issue",
            ticket_ref="#TEST-001",
        )
        row_id = entry.id

    # Read back in a fresh session.
    async with untenanted_session() as s:
        row = (
            await s.execute(select(InternalAccessLog).where(InternalAccessLog.id == row_id))
        ).scalar_one_or_none()

    assert row is not None, "emit_internal_access must persist to internal_access_log"
    assert row.staff_id == "ops-eng@example.com"
    assert row.approver_id == "lead-eng@example.com"
    assert row.admin_id == admin
    assert row.data_class == "leads"
    assert row.access_type == ACCESS_TYPE_BREAK_GLASS
    assert row.justification == "Customer-reported data issue"
    assert row.ticket_ref == "#TEST-001"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_internal_access_log_is_separate_from_admin_audit_log():
    """Axis11-F02: §5.11 requires internal_access_log to be separate from admin_audit_log.

    Writing an internal access entry must NOT create a row in admin_audit_log,
    and the row must only appear in internal_access_log.
    """
    admin = uuid.uuid4()
    now = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)

    audit_count_before: int = 0
    async with untenanted_session() as s:
        # Count admin_audit_log rows for this admin before the access event.
        audit_count_before = (
            (await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin)))
            .scalars()
            .all()
            .__len__()
        )

    async with untenanted_session() as s:
        await emit_internal_access(
            s,
            staff_id="support@example.com",
            admin_id=admin,
            access_type=ACCESS_TYPE_SUPPORT_SCOPED,
            access_started_at=now,
        )

    async with untenanted_session() as s:
        # internal_access_log must have 1 row for this admin.
        internal_rows = (
            (await s.execute(select(InternalAccessLog).where(InternalAccessLog.admin_id == admin)))
            .scalars()
            .all()
        )
        assert len(internal_rows) == 1, "internal_access_log must have exactly 1 row"

        # admin_audit_log must NOT have gained a new row from this operation.
        audit_rows = (
            (await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin)))
            .scalars()
            .all()
        )
        assert len(audit_rows) == audit_count_before, (
            "emit_internal_access must NOT write to admin_audit_log (§5.11 separation)"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_emit_internal_access_support_scoped():
    """Axis11-F02: support_scoped access type is accepted without approver."""
    now = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)

    async with untenanted_session() as s:
        entry = await emit_internal_access(
            s,
            staff_id="support@example.com",
            approver_id=None,  # optional for support_scoped
            access_type=ACCESS_TYPE_SUPPORT_SCOPED,
            access_started_at=now,
        )

    assert entry.access_type == ACCESS_TYPE_SUPPORT_SCOPED
    assert entry.approver_id is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_emit_internal_access_invalid_type_raises():
    """Axis11-F02: emit_internal_access must raise ValueError for unknown access_type."""
    now = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)

    async with untenanted_session() as s:
        with pytest.raises(ValueError, match="Invalid access_type"):
            await emit_internal_access(
                s,
                staff_id="ops@example.com",
                access_type="super_admin_override",  # not a valid type
                access_started_at=now,
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_emit_internal_access_all_fields_round_trip():
    """Axis11-F02: all fields are persisted and round-trip correctly."""
    admin = uuid.uuid4()
    instance = uuid.uuid4()
    started = datetime(2026, 6, 20, 9, 0, 0, tzinfo=UTC)
    ended = datetime(2026, 6, 20, 9, 30, 0, tzinfo=UTC)

    async with untenanted_session() as s:
        entry = await emit_internal_access(
            s,
            staff_id="staff@example.com",
            approver_id="approver@example.com",
            admin_id=admin,
            instance_id=instance,
            data_class="transcripts",
            access_started_at=started,
            access_ended_at=ended,
            access_type=ACCESS_TYPE_BREAK_GLASS,
            justification="Urgent customer escalation",
            ticket_ref="#ESC-999",
        )
        row_id = entry.id

    async with untenanted_session() as s:
        row = (
            await s.execute(select(InternalAccessLog).where(InternalAccessLog.id == row_id))
        ).scalar_one()

    assert row.staff_id == "staff@example.com"
    assert row.approver_id == "approver@example.com"
    assert row.admin_id == admin
    assert row.instance_id == instance
    assert row.data_class == "transcripts"
    assert row.access_type == ACCESS_TYPE_BREAK_GLASS
    assert row.justification == "Urgent customer escalation"
    assert row.ticket_ref == "#ESC-999"
    # Timestamps preserved
    assert row.access_started_at.replace(tzinfo=UTC) == started
    assert row.access_ended_at is not None
    assert row.access_ended_at.replace(tzinfo=UTC) == ended
