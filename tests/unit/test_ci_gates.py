"""CI quality-gate pins: steps in ci.yml that must keep the power to fail the build.

The secret scan once ran `detect-secrets scan --baseline`, which merges new findings
into the baseline and always exits 0, so the step could never fail. These tests pin
the shape that can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = doc["jobs"]["quality-gate"]["steps"]
    return steps


def _step(prefix: str) -> str:
    matches = [s for s in _steps() if str(s.get("name", "")).startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one ci.yml step named {prefix!r}"
    return str(matches[0]["run"])


def test_the_secret_scan_fails_on_a_secret_missing_from_the_baseline() -> None:
    run = _step("Secret scan (detect-secrets")
    # The pre-commit entry point exits 1 on any secret not in the baseline.
    assert "detect-secrets-hook" in run
    # `scan --baseline` always exits 0; it must not be what decides the step.
    commands = [ln.strip() for ln in run.splitlines() if not ln.strip().startswith("#")]
    assert not any("detect-secrets scan" in ln for ln in commands), (
        "the step must not rely on `detect-secrets scan`, which never fails"
    )
    # Every tracked file is scanned, except the baseline itself (its hashes are hex).
    assert "git ls-files -z -- ':!.secrets.baseline'" in run
    assert "xargs -0" in run
    # Exit 3 (line numbers refreshed, nothing new) is a pass, mapped inside each
    # xargs batch because xargs reports any non-zero exit as 123.
    assert '[ "$rc" -eq 3 ] && rc=0' in run
    # It works on a scratch copy so it never rewrites the committed baseline.
    assert 'cp .secrets.baseline "$RUNNER_TEMP/secrets.baseline"' in run
    assert '--baseline "$RUNNER_TEMP/secrets.baseline"' in run


def test_the_secret_scan_runs_on_every_trigger_including_the_daily_schedule() -> None:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert "schedule" in triggers
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]


def test_lint_and_format_cover_the_scripts_ci_and_deploys_run() -> None:
    # scripts/ runs in CI (check_doctrine_sync.py) and in every deploy (rollback,
    # reconcile, run_sql, task_definitions); it drifted while outside the lint scope.
    run = _step("Lint (ruff)")
    assert "ruff check app tests scripts" in run
    assert "ruff format --check app tests scripts" in run


def test_the_infra_job_type_checks_the_cdk_app_and_runs_its_jest_assertions() -> None:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = doc["jobs"]["infra"]
    assert job["defaults"]["run"]["working-directory"] == "infra"
    setup = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(setup) == 1
    assert setup[0]["with"]["node-version"] == "20"  # what deploy.yml synthesizes with
    assert setup[0]["with"]["cache-dependency-path"] == "infra/package-lock.json"
    runs = [str(s.get("run", "")) for s in job["steps"]]
    assert runs.index("npm ci") < runs.index("npx tsc --noEmit") < runs.index("npx jest")
    # A job-level `if:` would let a push or PR skip it.
    assert "if" not in job
