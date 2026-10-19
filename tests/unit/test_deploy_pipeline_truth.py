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
        ("dev", ["migrate", "smoke", "verify", "seedtest"]),
        ("prod-deploy", ["migrate", "smoke", "verify"]),
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
    # Services only — the schema is not reverted (migrations are expand-first).
    assert "update-service" in _run(steps[rollback_at])


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
    assert '"scripts/seed_and_livetest.py","--strict"' in seedtest
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
