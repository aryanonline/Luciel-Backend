"""Every dependency/CVE reporter posts to ONE monthly issue (round 7 WP-9).

The monthly steward routine (a cloud routine outside this repo) reads exactly one
issue a month, titled "Dependency & CVE review YYYY-MM". Three workflows report into
it — ci.yml's daily scheduled run (when it fails), image-rebuild.yml (weekly gate
report) and dependency-review.yml (the monthly review) — and before this round each
composed the title inline, and ci.yml's scheduled failures were posted nowhere. All
three now post through scripts/post_review_issue.sh; these tests pin that wiring, the
tracker evidence both CVE reporters attach, and the review's per-platform scan.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
POSTER = "scripts/post_review_issue.sh"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _runs(workflow: dict) -> list[str]:
    return [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("run")
    ]


@pytest.mark.parametrize("name", ["ci.yml", "image-rebuild.yml", "dependency-review.yml"])
def test_each_reporter_posts_through_the_one_poster_and_never_composes_a_title(name):
    runs = "\n".join(_runs(_workflow(name)))
    assert f"bash {POSTER} " in runs, f"{name} must post via {POSTER}"
    assert "gh issue create" not in runs and "gh issue comment" not in runs
    assert "Dependency & CVE review $" not in runs, f"{name} composes its own title"


def test_the_poster_uses_the_title_the_steward_reads_and_matches_it_exactly():
    script = (ROOT / POSTER).read_text(encoding="utf-8")
    assert 'title="Dependency & CVE review $month"' in script
    assert 'month="${REVIEW_MONTH:-$(date -u +%Y-%m)}"' in script
    assert 'select(.title == \\"$title\\")' in script, "substring search needs an exact match"
    assert "--label dependencies" in script


def test_ci_posts_only_a_scheduled_run_that_did_not_succeed():
    ci = _workflow("ci.yml")
    job = ci["jobs"]["report-scheduled-failure"]
    assert job["needs"] == "quality-gate"
    condition = job["if"]
    assert "always()" in condition
    assert "github.event_name == 'schedule'" in condition
    assert "needs.quality-gate.result != 'success'" in condition
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    # The workflow's other job keeps the default token: no workflow-level grant.
    assert "permissions" not in ci
    assert "17 6 * * *" in [entry["cron"] for entry in ci[True]["schedule"]]


@pytest.mark.parametrize("name", ["image-rebuild.yml", "dependency-review.yml"])
def test_both_cve_reporters_attach_debian_tracker_evidence(name):
    runs = "\n".join(_runs(_workflow(name)))
    assert "scripts/debian_tracker_status.py" in runs
    assert "--allowlist .security/cve-allowlist.json" in runs
    assert "--gate-output gate.txt" in runs
    assert "tracker.md" in runs


def test_the_tracker_step_runs_after_the_gate_and_can_never_fail_the_rebuild():
    steps = _workflow("image-rebuild.yml")["jobs"]["rebuild"]["steps"]
    names = [s.get("name", "") for s in steps]
    gate = next(i for i, s in enumerate(steps) if s.get("id") == "gate")
    tracker = next(i for i, n in enumerate(names) if n.startswith("Debian tracker status"))
    post = next(i for i, n in enumerate(names) if n.startswith("Post the outcome"))
    assert gate < tracker < post
    assert steps[tracker]["continue-on-error"] is True


def test_the_review_reads_the_platform_children_not_the_multi_arch_tag():
    """describe-image-scan-findings on the index TAG always said ScanNotFoundException."""
    review = _workflow("dependency-review.yml")
    scan = next(s for s in review["jobs"]["review"]["steps"] if s.get("id") == "scan")
    assert "bash scripts/gate_image_scan.sh" in scan["run"]
    assert "describe-image-scan-findings" not in scan["run"]
    assert "imageDetails[?imageTags]" in scan["run"], "children are untagged; pick a tag"
    assert "findingSeverityCounts" in scan["run"]
    assert scan["continue-on-error"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub for gh")
@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("64", "issue comment 64 --repo o/r --body-file"),
        ("null", "issue create --repo o/r --title Dependency & CVE review 2026-10 --label"),
    ],
)
def test_the_poster_comments_on_the_open_issue_or_opens_one(tmp_path, existing, expected):
    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")
    log = tmp_path / "gh.log"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        f'if [ "$1 $2" = "issue list" ]; then echo "{existing}"; fi\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    body = tmp_path / "body.md"
    body.write_text("hello", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "REPO": "o/r",
        "REVIEW_MONTH": "2026-10",
    }
    subprocess.run(["bash", str(ROOT / POSTER), str(body)], env=env, check=True)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("issue list --repo o/r --state open --search")
    assert expected in calls[1]
