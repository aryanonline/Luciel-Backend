"""Range drift for the monthly dependency review (round 7 WP-9, scripts/lock_drift.py).

With uv.lock committed, installs stop floating to the newest version a range allows,
so a lock nobody moves goes stale INSIDE its ranges. The monthly issue lists that gap
from `uv lock --upgrade --dry-run` (uv's joint resolution, nothing written).

The fixture is real `uv lock --upgrade --dry-run` output (uv 0.12.23, against a lock
written with `--exclude-newer 2026-10-10`, trimmed), plus one `Remove` line so every
line shape uv prints is covered.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location("lock_drift", _ROOT / "scripts" / "lock_drift.py")
assert _SPEC and _SPEC.loader
drift = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = drift  # dataclasses resolve their module by name
_SPEC.loader.exec_module(drift)

FIXTURE = Path(__file__).parent / "fixtures" / "uv_lock_upgrade_dry_run.txt"


def test_every_uv_line_shape_is_parsed():
    moves = {m.name: m for m in drift.parse_moves(FIXTURE.read_text(encoding="utf-8"))}
    assert moves["sqlalchemy"] == drift.Move("sqlalchemy", "update", "2.1.4", "2.1.5")
    assert moves["opentelemetry-api"].kind == "add"
    assert moves["six"] == drift.Move("six", "remove", "1.17.0", "—")
    assert "using" not in moves and "resolved" not in moves


def test_direct_dependencies_carry_their_pyproject_range_and_dev_is_marked(capsys, tmp_path):
    out = tmp_path / "drift.md"
    assert drift.main([str(FIXTURE), "--markdown", str(out)]) == 0
    report = out.read_text(encoding="utf-8")
    assert report.strip() == capsys.readouterr().out.strip()
    # Direct runtime deps, with the range that admits the newer version.
    assert "| sqlalchemy | 2.1.4 | 2.1.5 | `>=2.0.36` |" in report
    assert "| twilio | 9.11.2 | 9.12.0 | `>=9.0,<10` |" in report
    assert "| pydantic | 2.14.0 | 2.14.1 | `>=2.10` |" in report
    # A dev-extra dependency is marked as such.
    assert "| fakeredis | 2.39.0 | 2.40.0 | `>=2.20 (dev)` |" in report
    # Transitive packages are summarised, not tabled.
    assert "| botocore" not in report
    assert "botocore 1.43.111 → 1.43.114" in report
    assert "opentelemetry-api (new) 1.45.1" in report
    assert "six 1.17.0 (dropped)" in report
    assert report.startswith("7 direct and 6 transitive package(s)")


def test_a_lock_at_the_top_of_every_range_reports_no_drift():
    text = "Resolved 119 packages in 634ms\nNo lockfile changes detected\n"
    assert drift.render(text, {"fastapi": ">=0.138,<0.142"}).startswith("No drift")


def test_a_failed_dry_run_is_reported_not_mistaken_for_no_drift():
    text = "error: Failed to fetch: `https://pypi.org/simple/fastapi/`\n  Caused by: timeout\n"
    report = drift.render(text, {})
    assert report.startswith("Drift not computed")
    assert "Failed to fetch" in report


def test_an_unreadable_input_never_fails_the_job(tmp_path, capsys):
    assert drift.main([str(tmp_path / "missing.txt")]) == 0
    assert "Drift not computed" in capsys.readouterr().out


def test_the_shipped_pyproject_ranges_are_read():
    """The parser reads the ranges pyproject.toml ships TODAY, checked against an
    independent parse (``packaging``) — never a literal. Dependabot's uv updater moves
    the fastapi ceiling inside its weekly group PR, and a literal pin here turned every
    such PR red with auto-merge silently "leaving it" (PR #72, 2026-10-19; Phase 3
    review)."""
    pyproject = _ROOT / "pyproject.toml"
    ranges = drift.direct_ranges(pyproject)
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    shipped = {Requirement(raw).name: Requirement(raw).specifier for raw in project["dependencies"]}
    for name in ("fastapi", "psycopg"):
        assert set(ranges[name].split(",")) == {str(spec) for spec in shipped[name]}, name
    # The deliberate fastapi ceiling (a 0.137 bump once dropped include_router routes)
    # is still a ceiling, whatever version it names this week.
    assert any(spec.operator == "<" for spec in shipped["fastapi"])
    assert ranges["ruff"].endswith("(dev)")
