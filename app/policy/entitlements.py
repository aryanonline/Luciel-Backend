"""Entitlement matrix (DOCTRINE ANCHOR — Architecture §8, §3.3.1 requires_tier).

Single plan: every feature is available to every account; the only axis that
changes behavior above the free 50 is whether a payment method is on file
(Locked Decision §7 #12, #15). There is NO feature gating between under-cap and
PAYG accounts. This module is intentionally thin — it exists to make that
invariant explicit and testable, not to gate features.
"""

from __future__ import annotations

ALL_FEATURES_AVAILABLE_TO_ALL_ACCOUNTS = True


def feature_available(_feature: str) -> bool:
    """Every feature is available to every account (no tier gating)."""
    return True
