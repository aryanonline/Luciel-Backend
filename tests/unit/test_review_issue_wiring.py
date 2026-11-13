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

import json
import os
import re
import shutil
import subprocess
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
    assert '.title == \\"$title\\"' in script, "the list needs an exact-title match"
    assert "select(.pull_request == null" in script, "the REST list carries pull requests too"
    assert "--label dependencies" in script


def test_ci_posts_only_a_scheduled_run_that_did_not_succeed():
    ci = _workflow("ci.yml")
    job = ci["jobs"]["report-scheduled-failure"]
    # Both jobs the daily schedule runs: a red infra-only run must post too.
    assert job["needs"] == ["quality-gate", "infra"]
    condition = job["if"]
    assert "always()" in condition
    assert "github.event_name == 'schedule'" in condition
    assert "needs.quality-gate.result != 'success'" in condition
    assert "needs.infra.result != 'success'" in condition
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert '.name == "quality-gate" or .name == "infra"' in run, "jq must read both jobs"
    env = next(step["env"] for step in job["steps"] if step.get("env"))
    assert env["GATE_RESULT"] == "${{ needs.quality-gate.result }}"
    assert env["INFRA_RESULT"] == "${{ needs.infra.result }}"
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    # The workflow's other jobs keep the default token: no workflow-level grant.
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


def test_the_weekly_tag_cannot_collide_on_the_immutable_repo():
    """Run 39272257053 failed because `weekly-20261019` already existed: ECR tags are
    immutable there, so a second run that day, or a re-run, needs a tag of its own."""
    steps = _workflow("image-rebuild.yml")["jobs"]["rebuild"]["steps"]
    build = next(s for s in steps if s.get("id") == "build")
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in build["run"]
    assert 'TAG="weekly-' in build["run"]


def test_the_audit_headings_outcome_is_the_audits_not_tees():
    """The step's outcome becomes the issue's "pip-audit — **outcome**" heading. A `run:`
    with no `shell:` is `bash -e`: without pipefail `pip-audit | tee` reports tee's exit."""
    steps = _workflow("dependency-review.yml")["jobs"]["review"]["steps"]
    audit = next(s for s in steps if s.get("id") == "audit")
    assert "shell" not in audit
    run = audit["run"]
    assert "| tee audit.txt" in run
    assert run.index("set -o pipefail") < run.index("pip-audit --strict")


def test_the_review_reads_the_platform_children_not_the_multi_arch_tag():
    """describe-image-scan-findings on the index TAG always said ScanNotFoundException."""
    review = _workflow("dependency-review.yml")
    scan = next(s for s in review["jobs"]["review"]["steps"] if s.get("id") == "scan")
    assert "bash scripts/gate_image_scan.sh" in scan["run"]
    assert "describe-image-scan-findings" not in scan["run"]
    assert "imageDetails[?imageTags]" in scan["run"], "children are untagged; pick a tag"
    assert "findingSeverityCounts" in scan["run"]
    assert scan["continue-on-error"] is True


# A stand-in for gh that refuses what the real one refuses: `--slurp` with `--jq` is a
# usage error in gh itself (2.97.0 prints exactly this and exits 1), before any request.
# Every run of the three reporter workflows failed on it before this stub modelled it.
FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
if [ "$1" = "api" ]; then
  case " $* " in *" --slurp "*)
    case " $* " in *" --jq "* | *" -q "* | *" --template "* | *" -t "*)
      echo 'the `--slurp` option is not supported with `--jq` or `--template`' >&2
      exit 1 ;;
    esac ;;
  esac
  if [ -n "${FAKE_GH_API_FAILS:-}" ]; then
    echo "HTTP 502: Bad Gateway" >&2
    exit 1
  fi
  cat "$FAKE_GH_PAGES"
fi
"""
THE_TITLE = "Dependency & CVE review 2026-10"
# What `gh api --paginate --slurp` prints: one array per page.
SECOND_PAGE_HOLDS_IT = [
    [
        # A pull request is in the REST issues list too, and may carry the same title.
        {"number": 7, "title": THE_TITLE, "pull_request": {"url": "u"}},
        {"number": 8, "title": f"{THE_TITLE} (old)"},
    ],
    [{"number": 64, "title": THE_TITLE}],
]
NO_ISSUE_ONLY_LOOKALIKES = [
    [
        {"number": 7, "title": THE_TITLE, "pull_request": {"url": "u"}},
        {"number": 8, "title": "Dependency & CVE review 2026-09"},
    ]
]


def _run_poster(
    tmp_path: Path, pages: list[list[dict]], *, api_fails: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash, jq = shutil.which("bash"), shutil.which("jq")
    if bash is None or jq is None:
        pytest.skip("needs bash and jq on PATH (GitHub's runners have both)")
    log = tmp_path / "gh.log"
    pages_file = tmp_path / "pages.json"
    pages_file.write_text(json.dumps(pages), encoding="utf-8")
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_bytes(FAKE_GH.encode())
    fake.chmod(0o755)
    body = tmp_path / "body.md"
    body.write_text("hello", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_PAGES": str(pages_file),
        "REPO": "o/r",
        "REVIEW_MONTH": "2026-10",
    }
    if api_fails:
        env["FAKE_GH_API_FAILS"] = "1"
    done = subprocess.run(  # noqa: S603 — fixed argv, the repo's own script
        [bash, str(ROOT / POSTER), str(body)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return done, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        (SECOND_PAGE_HOLDS_IT, "issue comment 64 --repo o/r --body-file"),
        (NO_ISSUE_ONLY_LOOKALIKES, f"issue create --repo o/r --title {THE_TITLE} --label"),
        ([[]], f"issue create --repo o/r --title {THE_TITLE} --label"),
    ],
)
def test_the_poster_comments_on_the_open_issue_or_opens_one(tmp_path, pages, expected):
    done, calls = _run_poster(tmp_path, pages)
    assert done.returncode == 0, done.stderr
    # The REST issues list, never the search index (`issue list --search` AND
    # `--label` both go through it): it is eventually consistent, and two posters
    # firing on the same 1st would each open the month's issue.
    assert calls[0].startswith("api --paginate --slurp repos/o/r/issues?state=open")
    assert "--search" not in calls[0] and "--label" not in calls[0]
    assert "issue list" not in calls[0]
    assert expected in calls[1]
    assert len(calls) == 2


def test_a_failed_issue_list_opens_no_issue(tmp_path):
    """A list that failed is not a month with no issue: posting anyway would open a
    second one for the steward to miss."""
    done, calls = _run_poster(tmp_path, [[]], api_fails=True)
    assert done.returncode != 0
    assert len(calls) == 1 and calls[0].startswith("api ")


@pytest.mark.parametrize(
    "command",
    [
        'gh api --paginate --slurp "repos/o/r/issues" --jq ".[][]"',
        "gh api --slurp repos/o/r/issues -q .",
    ],
)
def test_the_stub_refuses_what_the_real_gh_refuses(tmp_path, command):
    """The stub is the only gh these tests have, so its refusal is pinned on its own."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash on PATH")
    fake = tmp_path / "gh"
    fake.write_bytes(FAKE_GH.encode())
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_PAGES": str(tmp_path / "unread.json"),
    }
    done = subprocess.run(  # noqa: S603 — fixed argv
        [bash, "-c", command], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 1
    assert "--slurp` option is not supported with `--jq`" in done.stderr


def _shell_commands(text: str) -> list[str]:
    """Commands with their backslash continuations joined, one per entry."""
    return text.replace("\r\n", "\n").replace("\\\n", " ").splitlines()


@pytest.mark.parametrize(
    "path",
    [*sorted(WORKFLOWS.glob("*.yml")), *sorted((ROOT / "scripts").glob("*.sh"))],
    ids=lambda p: p.name,
)
def test_no_gh_call_combines_slurp_with_a_jq_or_template_filter(path):
    for command in _shell_commands(path.read_text(encoding="utf-8")):
        if "gh api" in command and "--slurp" in command:
            assert not re.search(r"\s(--jq|-q|--template|-t)\s", command.split("|")[0]), command


def test_the_monthly_review_posts_well_before_the_steward_reads_the_issue():
    """The monthly steward routine (trig_018yawGTMcKu3vRbgVSy6SgQ, `0 13 1 * *`) reads
    the month's issue at 13:00 UTC on the 1st, and GitHub fires this repository's
    schedules 4-9 hours late. The review's cron must leave at least ten hours of
    headroom, or its drift and tracker sections land after their reader has been
    (2026-10-01: a 07:00 cron ran at 14:09Z)."""
    review = _workflow("dependency-review.yml")
    crons = [entry["cron"] for entry in review[True]["schedule"]]
    assert crons, "the monthly review must stay scheduled"
    for cron in crons:
        minute, hour, day = cron.split()[:3]
        assert day == "1", cron
        assert int(hour) <= 3, f"{cron}: too close to the steward's 13:00 UTC read"
        assert 0 <= int(minute) < 60, cron
