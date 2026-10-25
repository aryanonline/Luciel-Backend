"""Roll the app services (and the sweep schedules) back to the task definitions that
were serving before a deploy (round 7, Phase 3 infra review).

deploy.yml records the serving revisions before `cdk deploy`. The Compute stack then
replaces every task definition and, in its cleanup phase, DEREGISTERS the old
revisions - and ECS refuses to point a service at an INACTIVE revision (after a short
grace). A rollback that fired after a late gate (the §10 verify task, the strict
SeedTest) was therefore refused, and the services stayed on the code that had just
failed. This script re-registers a recorded revision that is no longer ACTIVE (an exact
copy, same family, same tags) and updates each service to it; the sweep schedules,
which the stack moved to the NEW SweepTask revision, are pointed back the same way.

    python3 scripts/rollback_services.py --env dev --cluster <cluster> \\
        --previous previous-services.json --sweep-task <recorded sweep revision or ''>

Every AWS call goes through one runner so the unit tests can replace it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_definitions import registerable  # noqa: E402

Runner = Callable[[list[str]], str]

#: ``get-schedule`` fields that ``update-schedule`` does not accept.
_SCHEDULE_READ_ONLY = ("Arn", "CreationDate", "LastModificationDate")
_SWEEP_SCHEDULE_PREFIX = "Luciel-{env}-sweep-"


def run_aws(args: list[str]) -> str:
    """Run one AWS CLI command and return its stdout (text); a failure raises."""
    done = subprocess.run(["aws", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise RuntimeError(f"aws {' '.join(args[:2])} failed with exit {done.returncode}")
    return done.stdout


def _cli_input(payload: dict[str, Any]) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(payload, handle)
    return f"file://{handle.name}"


def ensure_active(task_definition: str, *, region: str, aws: Runner) -> str:
    """The ARN of an ACTIVE revision equal to ``task_definition``: itself when it still
    is, else a freshly registered copy (same family, same tags)."""
    described = json.loads(
        aws(
            [
                "ecs",
                "describe-task-definition",
                "--region",
                region,
                "--task-definition",
                task_definition,
                "--include",
                "TAGS",
                "--output",
                "json",
            ]
        )
    )
    if described["taskDefinition"].get("status") == "ACTIVE":
        return task_definition
    new_arn = aws(
        [
            "ecs",
            "register-task-definition",
            "--region",
            region,
            "--cli-input-json",
            _cli_input(registerable(described)),
            "--query",
            "taskDefinition.taskDefinitionArn",
            "--output",
            "text",
        ]
    ).strip()
    print(f"[rollback] {task_definition} was INACTIVE; re-registered as {new_arn}", flush=True)
    return new_arn


def rollback_services(
    cluster: str, previous: list[dict[str, str]], *, region: str, aws: Runner
) -> None:
    for svc in previous:
        target = ensure_active(svc["td"], region=region, aws=aws)
        print(f"[rollback] {svc['name']} -> {target}", flush=True)
        aws(
            [
                "ecs",
                "update-service",
                "--region",
                region,
                "--cluster",
                cluster,
                "--service",
                svc["name"],
                "--task-definition",
                target,
                "--force-new-deployment",
                "--query",
                "service.deployments[0].status",
                "--output",
                "text",
            ]
        )


def schedule_update_input(schedule: dict[str, Any], task_definition: str) -> str:
    """The ``update-schedule`` input for ``schedule`` (a ``get-schedule`` body) with its ECS
    target moved to ``task_definition``; the read-only fields are dropped."""
    body = {k: v for k, v in schedule.items() if k not in _SCHEDULE_READ_ONLY}
    body["Target"]["EcsParameters"]["TaskDefinitionArn"] = task_definition
    return _cli_input(body)


def scheduler_may_run(
    role_arn: str, task_definition: str, cluster_arn: str, *, region: str, aws: Runner
) -> bool:
    """Whether ``role_arn`` (the schedules' role) is allowed ``ecs:RunTask`` on
    ``task_definition`` in ``cluster_arn`` - asked of IAM, not assumed. The role's grant
    used to name ONE revision; pointing the schedules at a re-registered copy then
    stopped every sweep silently (Phase 3 infra-fix review)."""
    result = json.loads(
        aws(
            [
                "iam",
                "simulate-principal-policy",
                "--region",
                region,
                "--policy-source-arn",
                role_arn,
                "--action-names",
                "ecs:RunTask",
                "--resource-arns",
                task_definition,
                "--context-entries",
                f"ContextKeyName=ecs:cluster,ContextKeyValues={cluster_arn},ContextKeyType=string",
                "--query",
                "EvaluationResults[0].EvalDecision",
                "--output",
                "json",
            ]
        )
    )
    return str(result) == "allowed"


def rollback_sweeps(env: str, sweep_task: str, *, region: str, aws: Runner) -> int:
    """Point every ``Luciel-<env>-sweep-*`` schedule back at ``sweep_task`` (re-registered
    when it is no longer ACTIVE) - unless the schedules' role may not RunTask it, in
    which case they are left on the deployed revision and that is said out loud.
    Returns how many schedules were updated."""
    if not sweep_task:
        print("[rollback] no sweep task definition was recorded; schedules left as deployed")
        return 0
    target = ensure_active(sweep_task, region=region, aws=aws)
    names = json.loads(
        aws(
            [
                "scheduler",
                "list-schedules",
                "--region",
                region,
                "--name-prefix",
                _SWEEP_SCHEDULE_PREFIX.format(env=env),
                "--query",
                "Schedules[].Name",
                "--output",
                "json",
            ]
        )
    )
    checked = False
    updated = 0
    for name in names:
        schedule = json.loads(
            aws(
                [
                    "scheduler",
                    "get-schedule",
                    "--region",
                    region,
                    "--name",
                    name,
                    "--output",
                    "json",
                ]
            )
        )
        if not checked:
            checked = True
            role = schedule["Target"]["RoleArn"]
            cluster_arn = schedule["Target"]["Arn"]
            if not scheduler_may_run(role, target, cluster_arn, region=region, aws=aws):
                print(
                    f"::warning::[rollback] the scheduler role {role} may not RunTask {target}; "
                    "the sweep schedules are LEFT on the deployed revision (the services were "
                    "rolled back). Deploy a release whose SweepTask grant covers the family."
                )
                return 0
        body = schedule_update_input(schedule, target)
        aws(["scheduler", "update-schedule", "--region", region, "--cli-input-json", body])
        print(f"[rollback] schedule {name} -> {target}", flush=True)
        updated += 1
    return updated


def main(argv: list[str] | None = None, *, aws: Runner = run_aws) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", required=True, help="environment name (dev|preprod|prod)")
    ap.add_argument("--cluster", required=True, help="the ECS cluster the services run in")
    ap.add_argument(
        "--previous", required=True, help="JSON list of {name, td} recorded before the deploy"
    )
    ap.add_argument(
        "--sweep-task",
        default="",
        help="the SweepTask revision recorded before the deploy ('' = none)",
    )
    ap.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ca-central-1",
    )
    args = ap.parse_args(argv)
    previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
    rollback_services(args.cluster, previous, region=args.region, aws=aws)
    updated = rollback_sweeps(args.env, args.sweep_task, region=args.region, aws=aws)
    print(f"[rollback] {len(previous)} service(s) and {updated} sweep schedule(s) restored")
    print(
        "[rollback] NOTE: the Compute stack still records the revisions it deployed; the "
        "next deploy's reconcile step moves the environment back onto them"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
