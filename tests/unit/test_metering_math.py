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


def test_meter_event_identifier_is_stable_for_the_same_watermark_span() -> None:
    """A retry recomputes the identifier from the same watermark span, so it must come
    out byte-identical or the additive meter counts the value twice. Distinct spans must
    stay distinct or genuinely new usage is deduped away and never billed."""
    from datetime import date

    ps = date(2026, 6, 1)
    span = {"customer_id": "cus_x", "period_start": ps}
    k1 = metering.meter_event_identifier(**span, from_block=1, to_block=2)
    k2 = metering.meter_event_identifier(**span, from_block=1, to_block=2)
    k3 = metering.meter_event_identifier(**span, from_block=2, to_block=3)
    # Same span, same period → same identifier → Stripe dedupes, no double-bill.
    assert k1 == k2
    assert k1 != k3  # the next block is a different span
    # A span of the same SIZE starting elsewhere is still a different event: 1→2 and 2→3
    # are both one block, and collapsing them would silently drop the second.
    assert (
        metering.meter_event_identifier(
            customer_id="cus_x", period_start=date(2026, 7, 1), from_block=1, to_block=2
        )
        != k1
    )


def test_the_block_count_only_ever_climbs_within_a_period() -> None:
    """Reporting posts the delta between the accrued total and what Stripe was told, so
    the count must be monotonic in the conversation count. A dip would make that delta
    negative, and a meter cannot take back a block the tenant really used — the post is
    simply skipped, so the usage would sit unbilled behind a watermark that never moves."""
    counts = [metering.billed_blocks(n, FREE, BLOCK) for n in range(0, 400)]
    assert counts == sorted(counts)
    assert counts[50] == 0  # inside the free 50, nothing billable
    assert counts[51] == 1  # block 1 opens
    assert counts[151] == 2
