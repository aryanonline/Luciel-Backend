"""Account-level notices rendered in the dashboard shell (Legal §A5).

§A5 is the binding case: reducing the free starter allowance owes every existing account
at least 30 days' notice delivered BOTH by email AND "as an in-dashboard notification
visible on next login" — the dashboard half exists precisely because a registered email
address may have gone stale. The dashboard renders this off the session payload, so a
notice source that produces nothing means the commitment is silently unmet.

The source is the platform announcement in settings rather than a table: a reduction is
one platform-wide event, identical for every tenant, so there is no per-account state to
store. Notices carry no tenant data, which is why this needs no session and no RLS.
Dismissal is visit-scoped in the client (Legal §A5 wants it back on next login), so
nothing here is persisted per account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings

ALLOWANCE_REDUCTION = "allowance_reduction"
PRICE_CHANGE = "price_change"
TERMS_CHANGE = "terms_change"

# Legal §A5 / §A10 minimum lead time between announcing a reduction and it taking effect.
NOTICE_LEAD_DAYS = 30

BILLING_HREF = "/billing"


@dataclass(frozen=True)
class AccountNotice:
    notice_id: str
    kind: str
    title: str
    body: str
    effective_at: datetime | None = None
    learn_more_href: str | None = None


def _parse_effective_at(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _allowance_reduction_notice(now: datetime) -> AccountNotice | None:
    settings = get_settings()
    raw = settings.free_allowance_reduction_effective_at
    new_allowance = settings.free_allowance_reduction_to
    if not raw or new_allowance <= 0 or new_allowance >= settings.free_allowance:
        return None
    effective_at = _parse_effective_at(raw)
    if effective_at is None or now >= effective_at:
        # Once the reduction has taken effect it is the current allowance, not news.
        return None
    days = max((effective_at - now).days, 0)
    return AccountNotice(
        # Keyed by the announcement, so re-rendering the same pending change never
        # resurrects a notice the Admin dismissed this visit, and moving the date
        # (a new announcement) correctly reads as a new notice.
        notice_id=f"{ALLOWANCE_REDUCTION}:{new_allowance}:{effective_at.date().isoformat()}",
        kind=ALLOWANCE_REDUCTION,
        title="Your free monthly conversation allowance is changing",
        body=(
            f"On {effective_at.date().isoformat()} the free starter allowance changes from "
            f"{settings.free_allowance} to {new_allowance} conversations per month "
            f"({days} days from now). Conversations beyond the allowance are billed at the "
            f"pay-as-you-go rate; you can review or change your billing setup at any time."
        ),
        effective_at=effective_at,
        learn_more_href=BILLING_HREF,
    )


def account_notices(*, now: datetime | None = None) -> list[AccountNotice]:
    """Notices to surface on this account's next login. Empty when nothing is pending."""
    now = now or datetime.now(UTC)
    notice = _allowance_reduction_notice(now)
    return [notice] if notice else []
