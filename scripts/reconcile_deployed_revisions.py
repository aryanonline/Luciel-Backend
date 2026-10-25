"""Make the serving revisions the stack's revisions after a deploy (round 7, Phase 3
infra-fix review).

The rollback (scripts/rollback_services.py) works OUT OF BAND: it points the services
and the sweep schedules at re-registered copies that CloudFormation does not know
about, while the Compute stack still records the revisions it deployed. A re-run of the
same commit then synthesizes an identical template, `cdk deploy` reports "(no changes)",
and the probes would pass against the rolled-back code - a green run on the previous
release. This step, run after every `cdk deploy` and before the probes, reads the
Compute stack's task-definition resources and moves every app service and every
``Luciel-<env>-sweep-*`` schedule that drifted back onto the stack's revision of the
same family, so what the probes test is what the pipeline says it deployed.

    python3 scripts/reconcile_deployed_revisions.py --env dev --cluster <cluster>

Exit 1 when a service's family has no task definition in the stack (the environment
is not the shape the stack describes - say so rather than probe it).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rollback_services import run_aws, schedule_update_input  # noqa: E402

Runner = Callable[[list[str]], str]


def family_of(task_definition_arn: str) -> str:
    """``.../task-definition/<family>:<revision>`` -> ``<family>``."""
    return task_definition_arn.rsplit("/", 1)[-1].split(":", 1)[0]


def stack_task_definitions(env: str, *, region: str, aws: Runner) -> dict[str, str]:
    """family -> the revision ARN the Compute stack currently records."""
    rows = json.loads(
        aws(
            [
                "cloudformation",
                "describe-stack-resources",
                "--region",
                region,
                "--stack-name",
                f"Luciel-{env}-Compute",
                "--query",
                "StackResources[?ResourceType=='AWS::ECS::TaskDefinition'].PhysicalResourceId",
                "--output",
                "json",
            ]
        )
    )
    return {family_of(arn): arn for arn in rows}


def reconcile_services(
    cluster: str, stack: dict[str, str], *, region: str, aws: Runner
) -> list[str]:
    """Move every service whose task definition is not the stack's revision of its family
    onto that revision. Returns the services moved."""
    arns = aws(
        [
            "ecs",
            "list-services",
            "--region",
            region,
            "--cluster",
            cluster,
            "--query",
            "serviceArns",
            "--output",
            "text",
        ]
    ).split()
    if not arns:
        return []
    services = json.loads(
        aws(
            [
                "ecs",
                "describe-services",
                "--region",
                region,
                "--cluster",
                cluster,
                "--services",
                *arns,
                "--query",
                "services[].{name:serviceName,td:taskDefinition}",
                "--output",
                "json",
            ]
        )
    )
    moved: list[str] = []
    for svc in services:
        family = family_of(svc["td"])
        wanted = stack.get(family)
        if wanted is None:
            raise SystemExit(
                f"[reconcile] {svc['name']} runs family {family}, which the Compute stack does not "
                "declare - the environment is not the shape the stack describes"
            )
        if svc["td"] == wanted:
            continue
        print(
            f"[reconcile] {svc['name']}: {svc['td']} -> {wanted} (the stack's revision)", flush=True
        )
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
                wanted,
                "--force-new-deployment",
                "--query",
                "service.deployments[0].status",
                "--output",
                "text",
            ]
        )
        moved.append(svc["name"])
    if moved:
        aws(
            [
                "ecs",
                "wait",
                "services-stable",
                "--region",
                region,
                "--cluster",
                cluster,
                "--services",
                *moved,
            ]
        )
    return moved


def reconcile_sweeps(env: str, stack: dict[str, str], *, region: str, aws: Runner) -> list[str]:
    """Point every ``Luciel-<env>-sweep-*`` schedule whose target is not the stack's
    revision of its family at that revision. Returns the schedules moved."""
    names = json.loads(
        aws(
            [
                "scheduler",
                "list-schedules",
                "--region",
                region,
                "--name-prefix",
                f"Luciel-{env}-sweep-",
                "--query",
                "Schedules[].Name",
                "--output",
                "json",
            ]
        )
    )
    moved: list[str] = []
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
        current = schedule["Target"]["EcsParameters"]["TaskDefinitionArn"]
        wanted = stack.get(family_of(current))
        if wanted is None:
            raise SystemExit(
                f"[reconcile] schedule {name} targets family {family_of(current)}, "
                "which the Compute stack does not declare"
            )
        if current == wanted:
            continue
        print(
            f"[reconcile] schedule {name}: {current} -> {wanted} (the stack's revision)", flush=True
        )
        body = schedule_update_input(schedule, wanted)
        aws(["scheduler", "update-schedule", "--region", region, "--cli-input-json", body])
        moved.append(name)
    return moved


def main(argv: list[str] | None = None, *, aws: Runner = run_aws) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", required=True, help="environment name (dev|preprod|prod)")
    ap.add_argument("--cluster", required=True, help="the ECS cluster the services run in")
    ap.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ca-central-1",
    )
    args = ap.parse_args(argv)
    stack = stack_task_definitions(args.env, region=args.region, aws=aws)
    services = reconcile_services(args.cluster, stack, region=args.region, aws=aws)
    sweeps = reconcile_sweeps(args.env, stack, region=args.region, aws=aws)
    if not services and not sweeps:
        print("[reconcile] every service and sweep schedule already runs the stack's revision")
    else:
        print(
            f"[reconcile] moved {len(services)} service(s) and {len(sweeps)} sweep schedule(s) "
            "back onto the stack's revisions (an earlier rollback had left them out of band)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
