"""The Today card tells a zoneless owner to set the business time zone (round 7 M10).

Live gap: a tenant with no ``instances.timezone`` booked, proposed and counted days in
UTC with nothing anywhere telling the owner. ``build_employee_status`` now serves the
``timezone_unset`` need (detail + an href to Configure) from the real rows, and it
disappears the moment a zone is set. The zone is never guessed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.employee.status import build_employee_status
from app.models.instance import Instance
from sqlalchemy import select

from tests.integration.test_employee_status import _seed

NOW = datetime.now(UTC)


async def _need_codes(admin: uuid.UUID, iid: uuid.UUID) -> dict[str, tuple[str, str]]:
    async with tenant_session(admin) as s:
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    return {n.code: (n.severity, n.href) for n in status.needs}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_zoneless_tenant_is_told_and_the_need_goes_once_a_zone_is_set():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(
            s,
            admin,
            iid,
            channels=[{"id": "widget", "enabled": True}],
            tools=[{"id": "book_appointment", "enabled": True}],
            timezone=None,
        )

    needs = await _need_codes(admin, iid)
    assert needs["timezone_unset"] == ("attention", "/dashboard/configure")

    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        inst.timezone = "America/Toronto"

    assert "timezone_unset" not in await _need_codes(admin, iid)
