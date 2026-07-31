"""Locked-number metering math (Vision §7, Arch §3.4.1b, BUILD_SPEC ~193).

Exhaustive at the 100-block boundaries: free 50; then $39 per 100 ABOVE 50,
rounded UP per 100-block. These are the locked numbers — the dollar amount comes
from the rounded-up blocks, while billedThisPeriod (wire) is the raw conversation
COUNT above free.
"""

from __future__ import annotations

import pytest
from app.billing import metering

FREE = 50
BLOCK = 100
PRICE = 3900  # $39.00 per 100-block (cents)


@pytest.mark.parametrize(
    ("counted", "expected_count"),
    [
        (0, 0),
        (50, 0),  # at free allowance → nothing above
        (51, 1),  # one over
        (240, 190),  # frozen payg seed: 240 - 50
    ],
)
def test_billed_conversations_count(counted: int, expected_count: int) -> None:
    assert metering.billed_conversations(counted, FREE) == expected_count


@pytest.mark.parametrize(
    ("counted", "expected_cents"),
    [
        (0, 0),
        (50, 0),  # 50 → $0 (locked)
        (51, 3900),  # 51 → $39 (1 block, locked)
        (100, 3900),  # 50 over → still 1 block → $39
        (150, 3900),  # 100 over → 1 block → $39 (locked)
        (151, 7800),  # 101 over → 2 blocks → $78 (locked)
        (250, 7800),  # 200 over → 2 blocks → $78
        (251, 11700),  # 201 over → 3 blocks → $117
    ],
)
def test_billed_cents_rounds_up_per_100_block(counted: int, expected_cents: int) -> None:
    assert metering.billed_cents(counted, FREE, BLOCK, PRICE) == expected_cents


@pytest.mark.parametrize(
    ("counted", "expected_blocks"),
    [(50, 0), (51, 1), (150, 1), (151, 2), (250, 2), (251, 3)],
)
def test_billed_blocks_boundaries(counted: int, expected_blocks: int) -> None:
    assert metering.billed_blocks(counted, FREE, BLOCK) == expected_blocks


def test_idempotency_key_stable_for_same_period_quantity() -> None:
    import uuid
    from datetime import date

    a = uuid.uuid4()
    ps = date(2026, 6, 1)
    k1 = metering.usage_idempotency_key(admin_id=a, period_start=ps, quantity=2)
    k2 = metering.usage_idempotency_key(admin_id=a, period_start=ps, quantity=2)
    k3 = metering.usage_idempotency_key(admin_id=a, period_start=ps, quantity=3)
    assert k1 == k2  # same (period, quantity) → same key → dedup, no double-bill
    assert k1 != k3  # different quantity → distinct key


@pytest.mark.parametrize(
    ("counted", "crosses"),
    [
        (0, False),
        (1, False),
        (50, False),  # still inside the free 50 — nothing billable yet
        (51, True),  # opens billable block 1
        (52, False),
        (150, False),
        (151, True),  # opens billable block 2
        (152, False),
        (251, True),  # block 3
    ],
)
def test_block_boundary_is_the_only_reporting_trigger(counted: int, crosses: bool) -> None:
    """One Stripe call per 100-block, not one per conversation. The reportable
    quantity only changes when a new block opens (Vision §7)."""
    assert metering.crosses_block_boundary(counted, FREE, BLOCK) is crosses


def test_boundary_crossings_line_up_with_the_block_count() -> None:
    """Every crossing increments billed_blocks by exactly one — so reporting only at
    crossings never skips a block."""
    crossings = [n for n in range(1, 400) if metering.crosses_block_boundary(n, FREE, BLOCK)]
    assert crossings == [51, 151, 251, 351]
    for n in crossings:
        assert (
            metering.billed_blocks(n, FREE, BLOCK) == metering.billed_blocks(n - 1, FREE, BLOCK) + 1
        )
