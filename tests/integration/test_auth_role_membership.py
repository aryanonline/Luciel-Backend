"""The app role cannot become the BYPASSRLS role (2026-09-05 audit F085).

``bootstrap_roles.sql`` grants ``luciel_app`` membership in ``luciel_auth`` so that
alembic — running as the app role — can reassign SECURITY DEFINER functions to it.
That is a migration-time need; at runtime the same membership meant any code path or
injected SQL could ``SET ROLE luciel_auth`` and switch off row-level security for
every tenant. The lock script (the post-migration ops step every environment runs,
and conftest runs here) revokes it, and the lock verifier fails loudly if it is back.
"""

from __future__ import annotations

import pytest
from app.db.session import untenanted_session
from app.lifecycle import audit_retention
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_app_role_cannot_set_role_to_the_bypassrls_role():
    async with untenanted_session() as s:
        member = (
            await s.execute(text("SELECT pg_has_role(current_user, 'luciel_auth', 'MEMBER')"))
        ).scalar_one()
        assert member is False
        with pytest.raises(Exception, match="permission denied"):
            await s.execute(text("SET ROLE luciel_auth"))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_lock_verifier_checks_the_membership():
    names = [c.name for c in audit_retention.AUDIT_LOCK_CHECKS]
    assert any("not a member of the BYPASSRLS role" in n for n in names)
    async with untenanted_session() as s:
        results = await audit_retention.audit_lock_results(s)
    failed = audit_retention.lock_failures(results)
    assert not [f for f in failed if "BYPASSRLS" in f], failed
