"""Pre-subject auth lookup — resolve an email to its account id.

Backed by the ``auth_lookup_account`` SECURITY DEFINER function (owned by the
narrow BYPASSRLS ``luciel_auth`` role) so the ONE lookup the auth flow needs
before a tenant subject exists can run. It returns only the resolving account's
own fields — never cross-tenant data. After resolution, all further work runs
under that account's ``app.admin_id`` (Layer 3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class AuthLookup:
    account_id: uuid.UUID
    password_hash: str
    state: str
    email_verified: bool
    session_epoch: int


async def lookup_account_by_email(session: AsyncSession, email: str) -> AuthLookup | None:
    row = (
        await session.execute(
            text(
                "SELECT account_id, password_hash, state, email_verified, session_epoch "
                "FROM auth_lookup_account(:email)"
            ),
            {"email": email},
        )
    ).one_or_none()
    if row is None:
        return None
    return AuthLookup(
        account_id=row.account_id,
        password_hash=row.password_hash,
        state=str(row.state),
        email_verified=row.email_verified,
        session_epoch=row.session_epoch,
    )
