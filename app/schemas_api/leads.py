"""Lead stewardship wire shapes (round 7 WP-3c, items 5 and 6).

``LeadOut`` itself lives beside the conversation shapes in ``conversations.py``
(the frozen admin client reads leads.* and conversations.* from one module); the
owner-initiated lead edits and the one-time CRM backfill added here get their own
request/response models so the lead surface's contract is in one place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas_api.common import WireModel

#: Why a lead was never pushed to a CRM (``LeadOut.crm_detail`` when
#: ``crm_status == "never_attempted"``). Derived at read time from the tenant's
#: CURRENT posture, so the dashboard can offer the one-time backfill the moment a
#: CRM connects (owner ruling 2026-09-14) instead of reading a stale reason.
NeverAttemptedReason = Literal[
    "crm_not_connected",  # no CRM connection, or one that is not connected
    "dormant",  # the CRM connection is billing-suspended (§A3)
    "tool_off",  # the owner switched the Push to CRM tool off
    "no_crm_key",  # the lead has no identifier the connected CRM accepts
    "not_yet_pushed",  # captured before the CRM connected — eligible for backfill
]

NEVER_ATTEMPTED_REASONS: frozenset[str] = frozenset(
    {"crm_not_connected", "dormant", "tool_off", "no_crm_key", "not_yet_pushed"}
)


class LeadPatchRequest(WireModel):
    """Owner edit of a lead's own facts (item 6). Every field is optional; a field
    that is omitted is left alone, and an explicit ``null`` clears it. Email and
    phone are normalised (case-folded / E.164) before they are stored."""

    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)


class CrmBackfillResult(WireModel):
    """What one bounded backfill pass did (item 5). ``remaining`` > 0 means another
    call is needed; every count is of LEADS, and none of the fields carries PII."""

    attempted: int
    synced: int
    failed: int
    skipped_no_key: int
    remaining: int
    # Why the pass ended early, in plain words: the broker refused before reaching
    # the CRM (tool off / connection not ready — nothing was attempted), or the CRM
    # rejected several pushes in a row and the pass stopped (phase-2 review; the
    # leads not reached are counted in ``remaining``). Null when the pass ran through.
    refused_reason: str | None = None
