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

There are two sources, in precedence order:

  1. ``platform_notices`` — a JSON array the operator PUBLISHES (also readable from the
     platform secret, so an announcement needs no redeploy). This is what makes §A10
     price changes and §B6 sub-processor additions publishable at all: their copy is
     operator-authored, so nothing could derive it.
  2. the DERIVED allowance-reduction notice, computed from the pending-reduction
     settings, whose copy the platform can write itself from the two numbers. It is
     appended only when the operator has not published their own §A5 notice.

Both are filtered on the same rule: a change that has already taken effect is the
current terms, not news.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWANCE_REDUCTION = "allowance_reduction"
PRICE_CHANGE = "price_change"
TERMS_CHANGE = "terms_change"
# 2026-09-05 audit F114: the model_currency sweep swapped a retired model. Admins
# are told, in the dashboard and by email, that the model behind their Luciel
# changed and that nothing is needed from them.
MODEL_UPDATED = "model_updated"
# The closed set AccountNoticeOut.kind accepts; anything else cannot be serialised.
KINDS: frozenset[str] = frozenset({ALLOWANCE_REDUCTION, PRICE_CHANGE, TERMS_CHANGE, MODEL_UPDATED})
# How long after an auto-swap the 'model updated' notice stays on the dashboard.
MODEL_UPDATED_NOTICE_DAYS = 14

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


def _model_updated_notice(now: datetime) -> AccountNotice | None:
    """The derived F114 notice, from the swap record the sweep wrote (LLM_MODEL_UPDATED).

    Fail-open: a malformed record is dropped with a warning, because this is read on
    the login path. The notice expires quietly after MODEL_UPDATED_NOTICE_DAYS."""
    raw = get_settings().llm_model_updated
    if not raw or not raw.strip():
        return None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("llm_model_updated is not valid JSON; publishing no notice")
        return None
    if not isinstance(record, dict):
        return None
    swapped_at = _parse_effective_at(str(record.get("at") or ""))
    new_model = str(record.get("to") or "")
    old_model = str(record.get("from") or "")
    if swapped_at is None or not new_model:
        logger.warning("llm_model_updated record is incomplete; publishing no notice")
        return None
    if now < swapped_at or (now - swapped_at).days >= MODEL_UPDATED_NOTICE_DAYS:
        return None
    return AccountNotice(
        notice_id=f"{MODEL_UPDATED}:{swapped_at.date().isoformat()}:{new_model}",
        kind=MODEL_UPDATED,
        title="Luciel's AI model was updated",
        body=(
            f"On {swapped_at.date().isoformat()} the AI provider retired the model Luciel "
            f"was running on ({old_model}), and the platform moved to its newest "
            f"same-family successor ({new_model}) after it passed a test turn. Your "
            "setup, knowledge and conversations are unchanged, and nothing is needed "
            "from you. If you notice a change in how Luciel answers, tell us."
        ),
        effective_at=None,
        learn_more_href=None,
    )


def _published_notices(now: datetime) -> list[AccountNotice]:
    """Parse the operator-published notice array from settings (Legal §A5 / §A10 / §B6).

    Fail-open by entry: a malformed announcement is dropped with a warning instead of
    raising, because this is read on the login path — a typo in one notice must not lock
    every Admin out of their dashboard. An unknown ``kind`` is dropped for the same
    reason it must be: ``AccountNoticeOut.kind`` is a closed Literal, so letting one
    through would fail response validation and 500 the session payload.
    """
    raw = get_settings().platform_notices
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("platform_notices is not valid JSON; publishing no notices")
        return []
    if not isinstance(parsed, list):
        logger.warning("platform_notices did not decode to a list; publishing no notices")
        return []

    out: list[AccountNotice] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            logger.warning("platform_notices entry is not an object; skipping it")
            continue
        notice_id = str(entry.get("noticeId") or "")
        kind = str(entry.get("kind") or "")
        title = str(entry.get("title") or "")
        body = str(entry.get("body") or "")
        if not notice_id or not title or not body or kind not in KINDS:
            logger.warning("platform_notices entry %r is incomplete; skipping it", notice_id)
            continue
        effective_raw = entry.get("effectiveAt")
        effective_at = _parse_effective_at(str(effective_raw)) if effective_raw else None
        # A change that has already landed is the current terms, not news (same rule the
        # derived allowance notice follows). A notice with no date runs until unpublished.
        if effective_at is not None and now >= effective_at:
            continue
        href = entry.get("learnMoreHref")
        out.append(
            AccountNotice(
                notice_id=notice_id,
                kind=kind,
                title=title,
                body=body,
                effective_at=effective_at,
                learn_more_href=str(href) if href else None,
            )
        )
    return out


def account_notices(*, now: datetime | None = None) -> list[AccountNotice]:
    """Notices to surface on this account's next login. Empty when nothing is pending."""
    now = now or datetime.now(UTC)
    published = _published_notices(now)
    # An operator-authored allowance-reduction notice SUPERSEDES the derived one: the
    # numbers are the same announcement, so surfacing both would double-banner it, and
    # the hand-written copy is the one the operator meant to send.
    derived = _allowance_reduction_notice(now)
    if derived is not None and not any(n.kind == ALLOWANCE_REDUCTION for n in published):
        published.append(derived)
    model_updated = _model_updated_notice(now)
    if model_updated is not None:
        published.append(model_updated)
    return published
