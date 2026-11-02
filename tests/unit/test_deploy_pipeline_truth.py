"""What the deploy pipeline does around a migration (round 7 review; reshaped WP-8b).

Deploy-safety properties, read straight from the files the pipeline runs, so a
refactor cannot quietly drop them:

* ``.github/workflows/deploy.yml`` — the MIGRATION RUNS BEFORE THE SERVICES ROLL.
  The MigrationTask definition lives in the Compute stack and carries the image, so
  the only migration used to run after ``cdk deploy`` had rolled both planes onto the
  new image: a refused migration left the NEW code serving the OLD schema.
  ``scripts/migrate-before-roll.sh`` now registers a revision of the current
  MigrationTask with the new image and migrates with it first; the post-roll run is a
  no-op safety (alembic is idempotent), and the rollback restores SERVICES only. The
  PROD job has the same previous/migrate/smoke/verify/rollback shape as dev (it had
  none of it), dev runs the strict D10 SeedTest gate, and every hostname comes from
  infra's hostnames helper (owner decision D6).
* ``scripts/migrate.sh`` — bootstrap grants the app role membership in the BYPASSRLS
  role for the migration's ownership transfers; the lock script revokes it on the
  success path. The revoke must also run when alembic fails, or the live app role is
  left able to ``SET ROLE`` past row-level security (audit F085).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
_PREROLL = ROOT / "scripts" / "migrate-before-roll.sh"


def _jobs() -> dict[str, dict[str, object]]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return dict(doc["jobs"])


def _steps(job: str) -> list[dict[str, object]]:
    return list(_jobs()[job]["steps"])  # type: ignore[call-overload]


def _index(steps: list[dict[str, object]], *, step_id: str | None = None, name: str = "") -> int:
    for i, s in enumerate(steps):
        if step_id is not None and s.get("id") == step_id:
            return i
        if step_id is None and name and name in str(s.get("name", "")):
            return i
    raise AssertionError(f"no step id={step_id!r} name~{name!r}")


def _run(step: dict[str, object]) -> str:
    return str(step.get("run", ""))


@pytest.mark.parametrize(("job", "env"), [("dev", "dev"), ("prod-deploy", "prod")])
def test_the_migration_runs_from_the_new_image_before_the_services_roll(job: str, env: str) -> None:
    steps = _steps(job)
    previous = _index(steps, step_id="previous")
    preroll = _index(steps, step_id="migrate_preroll")
    deploy = _index(steps, name="Deploy")
    post = _index(steps, step_id="migrate")
    # Record what is serving, migrate with the new image, THEN roll.
    assert previous < preroll < deploy < post
    run = _run(steps[preroll])
    assert f"scripts/migrate-before-roll.sh {env} " in run
    assert f"luciel-backend-{env}:$IMAGE_TAG" in run
    # The deploy role's own grants (RegisterTaskDefinition...) land first.
    grants = _index(steps, name="deploy role's grants first")
    assert grants < preroll
    assert f'"Luciel-{env}-Pipeline" --exclusively' in _run(steps[grants])
    # The post-roll run is the stack's own MigrationTask, kept as a no-op safety.
    assert f"scripts/run-migration-task.sh {env}" in _run(steps[post])


@pytest.mark.parametrize(
    ("job", "gates"),
    [
        ("dev", ["deploy", "migrate", "smoke", "verify", "seedtest"]),
        ("prod-deploy", ["deploy", "migrate", "smoke", "verify"]),
    ],
)
def test_a_failure_after_the_roll_restores_the_services(job: str, gates: list[str]) -> None:
    steps = _steps(job)
    rollback_at = _index(steps, name="Roll the app services back")
    condition = str(steps[rollback_at].get("if", ""))
    assert "failure()" in condition
    assert "steps.previous.outputs.had_previous == 'true'" in condition
    for gate in gates:
        assert f"steps.{gate}.outcome == 'failure'" in condition, gate
        assert _index(steps, step_id=gate) < rollback_at
    # A pre-roll refusal never rolled anything: there is nothing to restore.
    assert "migrate_preroll" not in condition
    # Services and sweep schedules — the schema is not reverted (migrations are
    # expand-first). The script re-registers a revision the Compute cleanup has
    # already deregistered (Phase 3 infra review): a bare update-service to an
    # INACTIVE revision is refused, which left the failed code serving.
    run = _run(steps[rollback_at])
    assert "scripts/rollback_services.py" in run and "update-service" not in run
    assert "--sweep-task '${{ steps.previous.outputs.sweep_td }}'" in run
    previous = steps[_index(steps, step_id="previous")]
    assert "scheduler get-schedule" in _run(previous) and "sweep_td=" in _run(previous)
    # The record step fails closed: a swallowed list-schedules left the schedules on
    # the failed revision while the run claimed them restored.
    assert "|| true" not in _run(previous) and "2>/dev/null" not in _run(previous)
    # The reconcile step runs after the post-roll migration and before the probes, and
    # is not a rollback trigger (a later gate still rolls back).
    reconcile = _index(steps, step_id="reconcile")
    assert _index(steps, step_id="migrate") < reconcile < _index(steps, step_id="smoke")
    assert "scripts/reconcile_deployed_revisions.py" in _run(steps[reconcile])
    assert "steps.reconcile.outcome" not in condition
    assert "SubscriptionArn" not in run  # the SNS check is its own, non-rolling step
    assert "steps.sns_confirmed.outcome" not in condition


@pytest.mark.parametrize(
    ("job", "env", "mode_expr"),
    [
        ("dev", "dev", "vars.DEV_INTEGRATION_MODE"),
        ("prod-deploy", "prod", "'live'"),
    ],
)
def test_every_live_deploy_proves_the_ses_topics_kept_a_confirmed_subscriber(
    job: str, env: str, mode_expr: str
) -> None:
    """Phase 3 infra review: SNS replaces a subscription whose endpoint changes and
    the SES routes confirm only in live mode; a topic without a confirmed subscriber
    drops inbound mail silently. The check runs after the probes, live only."""
    steps = _steps(job)
    check = steps[_index(steps, step_id="sns_confirmed")]
    assert _run(check).strip() == f"bash scripts/check_sns_subscriptions.sh {env}"
    assert mode_expr in str(check["if"]) and "== 'live'" in str(check["if"])
    assert _index(steps, step_id="verify") < _index(steps, step_id="sns_confirmed")
    assert _index(steps, step_id="sns_confirmed") < _index(steps, name="Roll the app services back")


def test_the_prod_job_has_the_dev_jobs_whole_shape() -> None:
    steps = _steps("prod-deploy")
    previous = steps[_index(steps, step_id="previous")]
    assert "Luciel-prod-ClusterName" in _run(previous)
    assert "previous-services.json" in _run(previous)
    smoke = _index(steps, step_id="smoke")
    verify = _index(steps, step_id="verify")
    assert _index(steps, step_id="migrate") < smoke < verify
    # Prod's smoke URL (and its mock Stripe posture) come from the hostnames helper.
    assert "scripts/smoke_probe.sh prod " in _run(steps[smoke])
    assert "scripts/run-verify-task.sh prod" in _run(steps[verify])
    # Prod's public records are only written once prod owns the public names (D6).
    hosts = _index(steps, step_id="hosts")
    assert hosts < _index(steps, name="Deploy")
    assert "servesUnownedPublicNames" in _run(steps[hosts])


def test_every_hostname_is_derived_never_written_into_a_command() -> None:
    """Owner decision D6: dev is api-dev.vantagemind.ai and the public names belong to
    whichever environment owns them. A literal host in a `run:` would probe (or
    deploy against) the owner instead of the environment being deployed."""
    for name, job in _jobs().items():
        for step in job.get("steps", []):  # type: ignore[union-attr]
            assert "vantagemind.ai" not in _run(step), (name, step.get("name"))
    dev = _steps("dev")
    assert "scripts/smoke_probe.sh dev " in _run(dev[_index(dev, step_id="smoke")])
    assert "bin/hostnames.ts dev" in _run(dev[_index(dev, step_id="hosts")])


_SMOKE = ROOT / "scripts" / "smoke_probe.sh"


def _edge_check(curl_answers: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the probe's own edge block (extracted, so the two cannot drift) against a
    stand-in curl that answers each URL with the given ``-w`` output."""
    text = _SMOKE.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("# --- edge check")
    block = text[start : text.index("# --- end edge check", start)]
    cases = "\n".join(
        f"    {url}) printf '%s' '{answer}' ;;" for url, answer in curl_answers.items()
    )
    harness = (
        "set -euo pipefail\n"
        "BASE=https://api.example\n"
        "sleep() { :; }\n"
        'curl() {\n  local url="${@: -1}"\n  case "$url" in\n'
        f"{cases}\n"
        "    *) echo unexpected-url >&2; exit 9 ;;\n  esac\n}\n"
        f"{block}\n"
    )
    bash = shutil.which("bash")
    assert bash, "the probe is a bash script"
    return subprocess.run(  # noqa: S603 — fixed argv, script text from this repo
        [bash, "-c", harness],
        capture_output=True,
        text=True,
        check=False,
    )


_EDGE_OK = {
    "http://api.example/healthz": "301 https://api.example:443/healthz",
    "https://api.example/openapi.json": "404",
    "https://api.example/docs": "404",
}


def test_the_probe_accepts_the_edge_the_docs_promise() -> None:
    result = _edge_check(_EDGE_OK)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout


@pytest.mark.parametrize(
    ("broken", "answer", "said"),
    [
        ("http://api.example/healthz", "200 ", "plain HTTP is answered"),
        ("http://api.example/healthz", "000 ", "plain HTTP is answered"),
        ("http://api.example/healthz", "301 http://elsewhere.example/", "redirects to"),
        ("https://api.example/openapi.json", "200", "the API schema is exposed"),
        ("https://api.example/docs", "200", "the API schema is exposed"),
    ],
)
def test_a_broken_edge_fails_the_probe_with_a_clear_line(
    broken: str, answer: str, said: str
) -> None:
    result = _edge_check({**_EDGE_OK, broken: answer})
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(
        line.startswith("::error::") and said in line and broken.split("//")[1] in line
        for line in result.stdout.splitlines()
    ), result.stdout


@pytest.mark.parametrize("job", ["dev", "prod-deploy"])
def test_every_deploy_runs_the_edge_check_and_a_failure_rolls_back(job: str) -> None:
    steps = _steps(job)
    assert "scripts/smoke_probe.sh" in _run(steps[_index(steps, step_id="smoke")])
    assert "edge_failures" in _SMOKE.read_text(encoding="utf-8")
    condition = str(steps[_index(steps, name="Roll the app services back")].get("if", ""))
    assert "steps.smoke.outcome == 'failure'" in condition


def test_no_message_tells_the_operator_to_deploy_dev_first() -> None:
    """Deploying dev first deletes the public records hours before prod can create them
    (the D6 handover rule): the refusal must not advise exactly that."""
    assert "deploy dev first" not in "\n".join(
        _run(step)
        for job in _jobs().values()
        for step in job.get("steps", [])  # type: ignore[union-attr]
    )


def test_d10_runs_the_seedtest_gate_strictly_after_every_live_dev_deploy() -> None:
    steps = _steps("dev")
    seedtest_at = _index(steps, step_id="seedtest")
    step = steps[seedtest_at]
    assert _run(step).strip() == "bash scripts/run-seedtest-task.sh dev --strict"
    # A non-zero exit fails the workflow: nothing may swallow it.
    assert not step.get("continue-on-error")
    assert _index(steps, step_id="verify") < seedtest_at
    condition = str(step.get("if", ""))
    assert "inputs.paused != 'true'" in condition and "== 'live'" in condition
    # A mock deploy says out loud that there was nothing live to gate.
    notice = next(s for s in steps if "a mock deploy has no live behavior" in str(s.get("name")))
    assert "::notice::" in _run(notice)
    assert "!= 'live'" in str(notice.get("if", ""))
    # The preprod release gate is strict too.
    verify = _steps("preprod-verify")
    assert any("scripts/run-seedtest-task.sh preprod --strict" in _run(s) for s in verify), (
        "the preprod §10 gate must run the harness with --strict"
    )


def test_a_prod_release_is_refused_before_preprod_while_prod_does_not_own_the_names() -> None:
    steps = _steps("preprod-standup")
    guard = _index(steps, name="Refuse a prod release")
    assert guard < _index(steps, name="Stand up preprod")
    assert "servesUnownedPublicNames" in _run(steps[guard])
    assert "exit 1" in _run(steps[guard])


# --- scripts/migrate-before-roll.sh ---------------------------------------------------


def _preroll_text() -> str:
    return _PREROLL.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_preroll_script_registers_runs_and_always_deregisters() -> None:
    text = _preroll_text()
    assert "MigrationTaskArn" in text
    assert "aws ecs describe-task-definition" in text
    assert "aws ecs register-task-definition" in text
    trap_at = text.index("trap deregister EXIT")
    run_at = text.index('run-migration-task.sh" "$ENVIRONMENT" "$new"')
    assert trap_at < run_at, "the deregister trap must be armed before the migration can fail"
    assert re.search(r"deregister\(\)\s*\{[^}]*deregister-task-definition", text)
    # First deploy: no export -> a visible notice, never a silent pass of a failure.
    assert "::notice::" in text and "exit 0" in text


def _transform(tmp_path: Path, described: dict[str, object]) -> subprocess.CompletedProcess[str]:
    text = _preroll_text()
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    code = text[start : text.index("\nPY\n", start)]
    src = tmp_path / "current.json"
    src.write_text(json.dumps(described), encoding="utf-8")
    return subprocess.run(  # noqa: S603 — the interpreter running this test, fixed argv
        [sys.executable, "-c", code, str(src), "reg/luciel-backend-dev:new", "MigrationContainer"],
        capture_output=True,
        text=True,
        check=False,
    )


def _described(*containers: dict[str, object]) -> dict[str, object]:
    return {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:ca-central-1:1:task-definition/Mig:7",
            "revision": 7,
            "status": "ACTIVE",
            "family": "LucieldevComputeMigrationTask",
            "taskRoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-TaskRole",
            "executionRoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-MigExec",
            "networkMode": "awsvpc",
            "containerDefinitions": list(containers),
            "requiresCompatibilities": ["FARGATE"],
            "compatibilities": ["EC2", "FARGATE"],
            "requiresAttributes": [{"name": "ecs.capability.execution-role-awslogs"}],
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "volumes": [],
            "registeredAt": "2026-10-19T00:00:00Z",
            "registeredBy": "arn:aws:sts::1:assumed-role/cfn",
        },
        "tags": [{"key": "Environment", "value": "dev"}],
    }


def test_the_revision_swaps_only_the_migration_image_and_keeps_what_register_accepts(
    tmp_path: Path,
) -> None:
    result = _transform(
        tmp_path,
        _described(
            {
                "name": "MigrationContainer",
                "image": "reg/luciel-backend-dev:old",
                "command": ["bash", "scripts/migrate.sh"],
            }
        ),
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["containerDefinitions"] == [
        {
            "name": "MigrationContainer",
            "image": "reg/luciel-backend-dev:new",
            "command": ["bash", "scripts/migrate.sh"],
        }
    ]
    # Same roles, platform and size as the revision it copies; cost tags kept.
    assert out["taskRoleArn"].endswith("Luciel-dev-Compute-TaskRole")
    assert out["runtimePlatform"]["cpuArchitecture"] == "ARM64"
    assert out["tags"] == [{"key": "Environment", "value": "dev"}]
    # Read-only describe fields that register-task-definition refuses are gone.
    for field in (
        "taskDefinitionArn",
        "revision",
        "status",
        "compatibilities",
        "requiresAttributes",
        "registeredAt",
        "registeredBy",
    ):
        assert field not in out, field


@pytest.mark.parametrize("count", [0, 2])
def test_the_revision_refuses_an_ambiguous_migration_container(tmp_path: Path, count: int) -> None:
    containers = [{"name": "MigrationContainer", "image": "x"} for _ in range(count)]
    if not containers:
        containers = [{"name": "SomethingElse", "image": "x"}]
    result = _transform(tmp_path, _described(*containers))
    assert result.returncode != 0
    assert result.stdout == ""


def test_the_runners_accept_the_preroll_revision_and_the_strict_flag() -> None:
    migration = (ROOT / "scripts" / "run-migration-task.sh").read_text(encoding="utf-8")
    assert 'TASKDEF_OVERRIDE="${2:-}"' in migration
    assert 'taskdef="$TASKDEF_OVERRIDE"' in migration
    seedtest = (ROOT / "scripts" / "run-seedtest-task.sh").read_text(encoding="utf-8")
    # The runner forwards every harness flag as a container command override and
    # adds --strict unless the caller chose --strict or --no-strict (WP-12).
    assert '"scripts/seed_and_livetest.py", *args' in seedtest
    assert '"--strict" not in args and "--no-strict" not in args' in seedtest
    assert '--overrides "$overrides"' in seedtest


# --- scripts/migrate.sh ---------------------------------------------------------------


def test_the_migration_script_revokes_the_migration_role_on_every_exit() -> None:
    script = (ROOT / "scripts" / "migrate.sh").read_text(encoding="utf-8")
    trap_at = script.index("trap revoke_migration_role EXIT")
    # The command line itself, not the header comment that also names it.
    alembic_at = script.index("\nalembic upgrade head")
    assert trap_at < alembic_at, "the trap must be armed before alembic can fail"
    assert re.search(r"revoke_migration_role\(\)\s*\{[^}]*revoke_migration_role\.sql", script)
    revoke = (ROOT / "scripts" / "revoke_migration_role.sql").read_text(encoding="utf-8")
    assert "REVOKE luciel_auth FROM luciel_app" in revoke
    # And the success path still verifies the revoke took (step 4 runs the lock checks).
    assert "verify_audit_lock.py" in script
