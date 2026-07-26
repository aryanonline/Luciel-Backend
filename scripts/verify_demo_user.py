"""One-off: verify a demo account's email directly (dev only).

Mirrors seed_and_livetest.mint_verify_token: reads the pending verify JTI from the DB
and marks the account verified, so a demo user can log in without a working SES email.
Runs in-VPC via the SeedTest task def (has DB access). Email passed via DEMO_EMAIL env.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select, update

from app.db.session import untenanted_session
from app.models.instance import Account, AccountState


async def main() -> None:
    email = os.environ["DEMO_EMAIL"].strip().lower()
    async with untenanted_session() as s:
        acct = (
            await s.execute(select(Account).where(Account.email_lower == email))
        ).scalar_one_or_none()
        if acct is None:
            print(f"[verify_demo] NO account for {email}")
            return
        await s.execute(
            update(Account)
            .where(Account.id == acct.id)
            .values(state=AccountState.verified, email_verified=True)
        )
        await s.commit()
        print(f"[verify_demo] {email} -> verified (admin_id={acct.id})")


if __name__ == "__main__":
    asyncio.run(main())
