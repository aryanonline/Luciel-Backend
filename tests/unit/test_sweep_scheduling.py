"""Every sweep kind must actually be scheduled (P0-8).

The defect P0-8 names is not a broken sweep — it is a correct one with no caller.
``reap_unverified_accounts`` was implemented, tested, and documented against §3.7.1a,
and nothing ever invoked it, so the "permanently deleted after 7 days" promise had
never run once in production. ``usage_reconcile`` was the same shape: an entrypoint and
a discovery branch, but no schedule.

Nothing in either half fails when they disagree. The Python side has a working sweep;
the CDK side synthesizes a valid stack; the gap is only visible by reading both. So
this test reads both — it is a wiring assertion, not a logic one, and it is the reason
adding a kind to ``SWEEP_KINDS`` without adding a schedule now fails CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.jobs.sweeps import SWEEP_KINDS, SWEEPS

_COMPUTE_STACK = Path(__file__).resolve().parents[2] / "infra" / "lib" / "compute-stack.ts"


def _scheduled_sweep_names() -> list[str]:
    """The ``name:`` of every entry in the CDK ``sweepSchedules`` array."""
    source = _COMPUTE_STACK.read_text(encoding="utf-8")
    block = re.search(r"const sweepSchedules[^=]*=\s*\[(.*?)\n    \];", source, re.DOTALL)
    assert block, f"could not find the sweepSchedules array in {_COMPUTE_STACK}"
    return re.findall(r"name:\s*'([a-z_]+)'", block.group(1))


def test_every_sweep_kind_has_an_eventbridge_schedule() -> None:
    unscheduled = set(SWEEP_KINDS) - set(_scheduled_sweep_names())
    assert not unscheduled, (
        f"sweeps that will never run in production: {sorted(unscheduled)}. "
        f"Add a schedule to sweepSchedules in {_COMPUTE_STACK.name}."
    )


def test_no_schedule_points_at_a_sweep_that_does_not_exist() -> None:
    """The mirror: a renamed kind leaves a schedule invoking ``python -m app.jobs <kind>``,
    which exits 2 nightly and only shows up in CloudWatch."""
    orphaned = set(_scheduled_sweep_names()) - set(SWEEP_KINDS)
    assert not orphaned, f"schedules with no matching sweep: {sorted(orphaned)}"


def test_the_dispatch_table_covers_every_declared_kind() -> None:
    """``python -m app.jobs`` dispatches through SWEEPS; a kind missing from it is
    rejected as a usage error at runtime, i.e. a nightly no-op."""
    assert set(SWEEPS) == set(SWEEP_KINDS)
