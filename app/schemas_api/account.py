"""Account status wire shapes — the closed-account reduced surface (owner decision D5).

``GET /api/v1/admin/account`` is the one read a closed account may make. It answers
for an open account too (``state`` verified, both instants null), so the frontend has
a single place to ask "what is this account, and until when can it be reopened?".
camelCase on the wire via ``WireModel``.
"""

from __future__ import annotations

from datetime import datetime

from app.schemas_api.auth import AccountStateWire
from app.schemas_api.common import WireModel


class AccountStatusOut(WireModel):
    state: AccountStateWire
    # When the owner closed the account, and the instant the 30-day window ends — the
    # last moment to reopen or re-request the export, and the first moment the purge
    # may run (§3.6.6 / Legal §B5). Both null on an open account.
    closed_at: datetime | None = None
    reopen_until: datetime | None = None
    # True only while the account is closed AND inside that window.
    can_reopen: bool = False
    # Whether reopening brings the Luciel back: the closure deleted it and it is still
    # in its grace window. False when there was no Luciel, or the owner had already
    # deleted it before closing (that deletion stays theirs — its own Restore applies).
    luciel_restored_on_reopen: bool = False


class ReopenResult(WireModel):
    """``POST /api/v1/admin/account/reopen`` — what the reopen actually did."""

    state: AccountStateWire
    # The Luciel came back from its grace window (to the state it was in before the
    # closure: active, or paused if it was paused).
    luciel_restored: bool = False
    luciel_state: str | None = None
    # ``payg_enabled`` | ``free_cap`` — the billing arrangement the account resumes on.
    billing_state: str
