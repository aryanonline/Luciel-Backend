"""The deploy pipeline's rollback restores what was serving (round 7, Phase 3 infra review).

CloudFormation deregisters the previous task-definition revisions in the Compute
stack's cleanup, and ECS refuses to point a service at an INACTIVE revision. The
rollback used to call update-service with the recorded (now INACTIVE) ARN and be
refused - after a late gate, the services stayed on the code that had just failed.
scripts/rollback_services.py re-registers such a revision as an exact copy and
updates the services to it; the sweep schedules the stack moved to the NEW SweepTask
revision are pointed back the same way.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_TD = "arn:aws:ecs:ca-central-1:1:task-definition"
sys.path.insert(0, str(_ROOT / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rollback = _load("rollback_services")
task_definitions = _load("task_definitions")

_DESCRIBED = {
    "taskDefinition": {
        "taskDefinitionArn": "arn:aws:ecs:ca-central-1:1:task-definition/dataplane:95",
        "family": "dataplane",
        "revision": 95,
        "status": "INACTIVE",
        "registeredAt": "2026-10-19T04:29:00Z",
        "registeredBy": "arn:aws:sts::1:assumed-role/cdk",
        "compatibilities": ["EC2", "FARGATE"],
        "requiresAttributes": [{"name": "ecs.capability.execution-role-ecr-pull"}],
        "taskRoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-TaskRole",
        "executionRoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-Exec",
        "networkMode": "awsvpc",
        "containerDefinitions": [
            {"name": "data-planeContainer", "image": "ecr/luciel-backend-dev:6974449"}
        ],
        "volumes": [],
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    },
    "tags": [{"key": "Environment", "value": "dev"}],
}


class _FakeAws:
    """Answers the CLI calls the script makes and records every argv."""

    def __init__(self, *, active: set[str], schedules: list[str]) -> None:
        self.active = active
        self.schedules = schedules
        self.calls: list[list[str]] = []
        self.registered: list[dict] = []
        self.updated_schedules: list[dict] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        service, verb = args[0], args[1]
        if (service, verb) == ("ecs", "describe-task-definition"):
            arn = args[args.index("--task-definition") + 1]
            described = json.loads(json.dumps(_DESCRIBED))
            described["taskDefinition"]["taskDefinitionArn"] = arn
            described["taskDefinition"]["family"] = arn.rsplit("/", 1)[1].split(":")[0]
            described["taskDefinition"]["status"] = "ACTIVE" if arn in self.active else "INACTIVE"
            return json.dumps(described)
        if (service, verb) == ("ecs", "register-task-definition"):
            path = args[args.index("--cli-input-json") + 1].removeprefix("file://")
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.registered.append(payload)
            return f"arn:aws:ecs:ca-central-1:1:task-definition/{payload['family']}:97\n"
        if (service, verb) == ("ecs", "update-service"):
            return "PRIMARY\n"
        if (service, verb) == ("scheduler", "list-schedules"):
            prefix = args[args.index("--name-prefix") + 1]
            return json.dumps([n for n in self.schedules if n.startswith(prefix)])
        if (service, verb) == ("scheduler", "get-schedule"):
            name = args[args.index("--name") + 1]
            return json.dumps(
                {
                    "Arn": f"arn:aws:scheduler:ca-central-1:1:schedule/default/{name}",
                    "CreationDate": "2026-09-01T00:00:00Z",
                    "LastModificationDate": "2026-10-19T04:30:00Z",
                    "Name": name,
                    "GroupName": "default",
                    "ScheduleExpression": "cron(55 * * * ? *)",
                    "ScheduleExpressionTimezone": "UTC",
                    "FlexibleTimeWindow": {"Mode": "OFF"},
                    "State": "ENABLED",
                    "Target": {
                        "Arn": "arn:aws:ecs:ca-central-1:1:cluster/luciel-dev",
                        "RoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-SweepSchedulerRole",
                        "EcsParameters": {
                            "TaskDefinitionArn": f"{_TD}/sweep:40",
                            "LaunchType": "FARGATE",
                            "TaskCount": 1,
                        },
                        "Input": json.dumps({"sweep": name.rsplit("-", 1)[-1]}),
                        "RetryPolicy": {
                            "MaximumEventAgeInSeconds": 86400,
                            "MaximumRetryAttempts": 185,
                        },
                    },
                }
            )
        if (service, verb) == ("scheduler", "update-schedule"):
            path = args[args.index("--cli-input-json") + 1].removeprefix("file://")
            self.updated_schedules.append(json.loads(Path(path).read_text(encoding="utf-8")))
            return "{}"
        raise AssertionError(f"unexpected aws call: {args[:3]}")


def test_registerable_keeps_only_what_register_accepts_plus_the_tags() -> None:
    out = task_definitions.registerable(_DESCRIBED)
    assert set(out) == {
        "family",
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "containerDefinitions",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "runtimePlatform",
        "tags",
    }
    assert "taskDefinitionArn" not in out and "status" not in out and "revision" not in out
    assert out["tags"] == [{"key": "Environment", "value": "dev"}]
    assert out["containerDefinitions"][0]["image"] == "ecr/luciel-backend-dev:6974449"


def test_an_inactive_revision_is_re_registered_before_the_service_is_updated(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous-services.json"
    previous.write_text(
        json.dumps(
            [
                {
                    "name": "Luciel-dev-Compute-DataPlane",
                    "td": "arn:aws:ecs:ca-central-1:1:task-definition/dataplane:95",
                },
                {
                    "name": "Luciel-dev-Compute-ControlPlane",
                    "td": "arn:aws:ecs:ca-central-1:1:task-definition/controlplane:95",
                },
            ]
        ),
        encoding="utf-8",
    )
    aws = _FakeAws(
        active={"arn:aws:ecs:ca-central-1:1:task-definition/controlplane:95"}, schedules=[]
    )
    assert (
        rollback.main(
            [
                "--env",
                "dev",
                "--cluster",
                "luciel-dev",
                "--previous",
                str(previous),
                "--region",
                "ca-central-1",
            ],
            aws=aws,
        )
        == 0
    )
    # The INACTIVE data-plane revision was copied (family kept, read-only fields dropped) ...
    assert [r["family"] for r in aws.registered] == ["dataplane"]
    updates = [c for c in aws.calls if c[:2] == ["ecs", "update-service"]]
    targets = {c[c.index("--service") + 1]: c[c.index("--task-definition") + 1] for c in updates}
    # ... and the service now points at the copy; the still-ACTIVE one is used as is.
    assert targets == {
        "Luciel-dev-Compute-DataPlane": f"{_TD}/dataplane:97",
        "Luciel-dev-Compute-ControlPlane": f"{_TD}/controlplane:95",
    }
    assert all("--force-new-deployment" in c for c in updates)


def test_the_sweep_schedules_are_pointed_back_at_the_recorded_revision(tmp_path: Path) -> None:
    previous = tmp_path / "previous-services.json"
    previous.write_text("[]", encoding="utf-8")
    aws = _FakeAws(
        active=set(),
        schedules=[
            "Luciel-dev-sweep-daily_brief",
            "Luciel-dev-sweep-deferred",
            "Luciel-prod-sweep-deferred",
        ],
    )
    assert (
        rollback.main(
            [
                "--env",
                "dev",
                "--cluster",
                "luciel-dev",
                "--previous",
                str(previous),
                "--sweep-task",
                "arn:aws:ecs:ca-central-1:1:task-definition/sweep:40",
                "--region",
                "ca-central-1",
            ],
            aws=aws,
        )
        == 0
    )
    # Only this environment's schedules, each updated with the full get-schedule body
    # minus what update-schedule refuses, and the recorded (re-registered) revision.
    assert [s["Name"] for s in aws.updated_schedules] == [
        "Luciel-dev-sweep-daily_brief",
        "Luciel-dev-sweep-deferred",
    ]
    for body in aws.updated_schedules:
        assert (
            body["Target"]["EcsParameters"]["TaskDefinitionArn"]
            == "arn:aws:ecs:ca-central-1:1:task-definition/sweep:97"
        )
        assert not {"Arn", "CreationDate", "LastModificationDate"} & set(body)
        assert body["FlexibleTimeWindow"] == {"Mode": "OFF"} and body["Target"]["Input"]


def test_no_recorded_sweep_revision_leaves_the_schedules_alone(tmp_path: Path, capsys) -> None:
    previous = tmp_path / "previous-services.json"
    previous.write_text("[]", encoding="utf-8")
    aws = _FakeAws(active=set(), schedules=["Luciel-dev-sweep-deferred"])
    assert (
        rollback.main(
            ["--env", "dev", "--cluster", "c", "--previous", str(previous), "--sweep-task", ""],
            aws=aws,
        )
        == 0
    )
    assert not [c for c in aws.calls if c[0] == "scheduler"]
    assert "schedules left as deployed" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["ACTIVE", "INACTIVE"])
def test_ensure_active_returns_the_same_arn_only_while_it_is_active(status: str) -> None:
    arn = "arn:aws:ecs:ca-central-1:1:task-definition/sweep:40"
    aws = _FakeAws(active={arn} if status == "ACTIVE" else set(), schedules=[])
    out = rollback.ensure_active(arn, region="ca-central-1", aws=aws)
    assert out == (
        arn if status == "ACTIVE" else "arn:aws:ecs:ca-central-1:1:task-definition/sweep:97"
    )
