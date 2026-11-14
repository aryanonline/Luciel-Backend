"""The AWS account id and the operator inbox are repository settings, not repository text.

Portfolio clean-up (2026-11-14), read straight from the workflow files:

* No workflow carries the 12-digit AWS account id. Every role ARN reads the
  ``AWS_ACCOUNT_ID`` repository variable, and every job that assumes a role checks that
  variable before it does anything else, failing with an ``::error::`` line that names
  it (the monthly review, whose AWS half is advisory, reports it and still posts).
* A repository variable is not a secret. GitHub prints its value unmasked in run logs
  (the role ARN, the check step's env header), and AWS does not treat an account id as
  secret: the variable keeps the id out of the repository's text, not out of the logs.
  It is read only where it is used, never from a workflow- or job-level ``env``, whose
  values every step's log header repeats.
* deploy.yml used to inline ``-c budgetAlertEmail=${{ vars.BUDGET_ALERT_EMAIL || '' }}``
  unquoted into its scripts: GitHub printed the address in every run log, and a value
  holding shell syntax would have run as code. Every ``cdk deploy`` and ``cdk destroy``
  now reads the ``BUDGET_ALERT_EMAIL`` secret from its step environment, quoted. The
  preprod standup and teardown passed no inbox at all, and ObservabilityStack refuses to
  synthesise an armed environment without one, so the release flow could not have
  synthesised preprod; every job that synthesises now checks the secret first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ACCOUNT_VAR = "${{ vars.AWS_ACCOUNT_ID }}"
INBOX_SECRET = "${{ secrets.BUDGET_ALERT_EMAIL }}"
INBOX_ARG = '-c budgetAlertEmail="$BUDGET_ALERT_EMAIL"'
CONFIGURE_AWS = "aws-actions/configure-aws-credentials"
FAKE_ACCOUNT = "111111111111"
FAKE_INBOX = "ops@example.com"

# Every job that assumes an AWS role, and the deploy jobs that synthesise the CDK app.
ROLE_JOBS = {
    ("dependency-review.yml", "review"),
    ("deploy.yml", "dev"),
    ("deploy.yml", "preprod-standup"),
    ("deploy.yml", "preprod-verify"),
    ("deploy.yml", "prod-deploy"),
    ("deploy.yml", "preprod-teardown"),
    ("dev-cost-guard.yml", "restop-db"),
    ("image-rebuild.yml", "rebuild"),
}
SYNTH_JOBS = ["dev", "preprod-standup", "prod-deploy", "preprod-teardown"]
_CDK = re.compile(r"\bcdk (?:deploy|destroy)\b")


def _names() -> list[str]:
    return sorted(p.name for p in WORKFLOWS.glob("*.yml"))


def _workflow(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return doc


def _steps(name: str, job: str) -> list[dict[str, Any]]:
    return list(_workflow(name)["jobs"][job]["steps"])


def _assumes_a_role(step: dict[str, Any]) -> bool:
    return CONFIGURE_AWS in str(step.get("uses", ""))


def _account_check(steps: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    for at, step in enumerate(steps):
        if step.get("env", {}).get("AWS_ACCOUNT_ID") == ACCOUNT_VAR and "run" in step:
            return at, step
    raise AssertionError("no step checks the AWS_ACCOUNT_ID variable")


def _code(run: str) -> str:
    """A step's script without its comment lines."""
    return "\n".join(line for line in run.splitlines() if not line.strip().startswith("#"))


def _run_check(step: dict[str, Any], account: str, inbox: str) -> subprocess.CompletedProcess[str]:
    """Run a step's script the way the runner runs a `run:` with no shell (`bash -e`)."""
    bash = shutil.which("bash")
    assert bash, "workflow scripts are bash"
    return subprocess.run(  # noqa: S603 — fixed argv, script text from this repo
        [bash, "-e", "-c", str(step["run"])],
        env={**os.environ, "AWS_ACCOUNT_ID": account, "BUDGET_ALERT_EMAIL": inbox},
        capture_output=True,
        text=True,
        check=False,
    )


def _errors_naming(result: subprocess.CompletedProcess[str], setting: str) -> list[str]:
    return [
        line
        for line in result.stdout.splitlines()
        if line.startswith("::error::") and setting in line
    ]


@pytest.mark.parametrize("name", _names())
def test_no_workflow_carries_an_aws_account_id(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), f"{name}: a 12-digit account id"


def test_every_role_arn_reads_the_account_variable() -> None:
    found = set()
    for name in _names():
        for job_id, job in _workflow(name)["jobs"].items():
            for step in job.get("steps", []):
                if not _assumes_a_role(step):
                    continue
                found.add((name, job_id))
                arn = str(step["with"]["role-to-assume"])
                assert arn.startswith(f"arn:aws:iam::{ACCOUNT_VAR}:role/"), (name, arn)
    assert found == ROLE_JOBS


def test_no_workflow_or_job_env_carries_the_account_id() -> None:
    """GitHub does not mask a variable, and every step's log header repeats these envs.

    deploy.yml set a workflow-level ``ACCOUNT`` that nothing read, and image-rebuild.yml
    one that only its role ARN read, so every step of every job printed the id.
    """
    for name in _names():
        doc = _workflow(name)
        assert "AWS_ACCOUNT_ID" not in str(doc.get("env", {})), name
        for job_id, job in doc["jobs"].items():
            assert "AWS_ACCOUNT_ID" not in str(job.get("env", {})), (name, job_id)


@pytest.mark.parametrize(("name", "job"), sorted(ROLE_JOBS))
def test_every_job_that_assumes_a_role_checks_the_account_variable_first(
    name: str, job: str
) -> None:
    steps = _steps(name, job)
    at, check = _account_check(steps)
    assert at < next(i for i, s in enumerate(steps) if _assumes_a_role(s))
    for bad in ("", "98765", f"{FAKE_ACCOUNT} "):
        result = _run_check(check, bad, FAKE_INBOX)
        assert result.returncode != 0, (bad, result.stdout)
        assert _errors_naming(result, "AWS_ACCOUNT_ID"), result.stdout
        # The script names the setting and never echoes the value it was given. (The
        # runner still prints the step's env header, which this test cannot see.)
        assert not bad or bad.strip() not in result.stdout, result.stdout
    ok = _run_check(check, FAKE_ACCOUNT, FAKE_INBOX)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "::error::" not in ok.stdout
    assert FAKE_ACCOUNT not in ok.stdout
    if name == "dependency-review.yml":
        # Advisory AWS half: the review reports the missing variable and still posts.
        assert check["continue-on-error"] is True
        configure = next(s for s in steps if _assumes_a_role(s))
        assert configure["if"] == f"steps.{check['id']}.outcome == 'success'"
    else:
        assert not check.get("continue-on-error"), "a missing account id fails the job"


def test_deploy_reads_the_inbox_from_the_secret_never_from_script_text() -> None:
    text = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    assert "vars.BUDGET_ALERT_EMAIL" not in text
    assert not re.search(r"budgetAlertEmail=\$\{\{", text), "an inlined expression is code"
    synthesising = set()
    for job_id, job in _workflow("deploy.yml")["jobs"].items():
        for step in job.get("steps", []):
            code = _code(str(step.get("run", "")))
            commands = len(_CDK.findall(code))
            if not commands:
                continue
            synthesising.add(job_id)
            # One quoted inbox argument per cdk command, read from the step's own env.
            assert code.count(INBOX_ARG) == commands, (job_id, step.get("name"))
            assert step.get("env", {}).get("BUDGET_ALERT_EMAIL") == INBOX_SECRET, step.get("name")
    assert synthesising == set(SYNTH_JOBS)


@pytest.mark.parametrize(
    ("job", "step_name"),
    [
        ("preprod-standup", "Stand up preprod"),
        ("preprod-teardown", "Tear down ephemeral preprod"),
    ],
)
def test_the_preprod_standup_and_teardown_pass_the_inbox(job: str, step_name: str) -> None:
    """Both synthesise an armed preprod, and neither passed an inbox before."""
    step = next(s for s in _steps("deploy.yml", job) if step_name in str(s.get("name", "")))
    assert INBOX_ARG in _code(str(step["run"]))


@pytest.mark.parametrize("job", SYNTH_JOBS)
def test_every_job_that_synthesises_checks_the_inbox_before_anything_runs(job: str) -> None:
    check = _steps("deploy.yml", job)[0]
    assert check.get("env", {}).get("BUDGET_ALERT_EMAIL") == INBOX_SECRET
    assert not check.get("continue-on-error")
    missing = _run_check(check, FAKE_ACCOUNT, "")
    assert missing.returncode == 1, missing.stdout
    assert _errors_naming(missing, "BUDGET_ALERT_EMAIL"), missing.stdout
    # Both settings missing: one run names both, so a first setup is one round trip.
    both = _run_check(check, "", "")
    assert both.returncode == 1
    assert _errors_naming(both, "AWS_ACCOUNT_ID") and _errors_naming(both, "BUDGET_ALERT_EMAIL")
    ok = _run_check(check, FAKE_ACCOUNT, FAKE_INBOX)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert FAKE_INBOX not in ok.stdout + missing.stdout + both.stdout
