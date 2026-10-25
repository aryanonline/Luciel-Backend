"""After every deploy the serving revisions are the Compute stack's (round 7, Phase 3
infra-fix review).

The rollback works out of band - re-registered copies the stack does not know about.
A re-run of the same commit then synthesizes an identical template, cdk reports no
changes, and the probes would pass against the rolled-back code. The reconcile step
moves every service and every sweep schedule that drifted back onto the stack's
revision of its family before anything is probed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
_TD = "arn:aws:ecs:ca-central-1:1:task-definition"
_STACK = {
    "LucieldevComputedataplaneTask7C85D254": f"{_TD}/LucieldevComputedataplaneTask7C85D254:96",
    "LucieldevComputecontrolplaneTaskE44E4C8B": (
        f"{_TD}/LucieldevComputecontrolplaneTaskE44E4C8B:96"
    ),
    "LucieldevComputeSweepTask2F33BE67": f"{_TD}/LucieldevComputeSweepTask2F33BE67:58",
    "LucieldevComputeMigrationTaskBE808E33": f"{_TD}/LucieldevComputeMigrationTaskBE808E33:92",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("rollback_services")
reconcile = _load("reconcile_deployed_revisions")


class _FakeAws:
    def __init__(self, services: dict[str, str], schedules: dict[str, str]) -> None:
        self.services = services  # name -> current task definition
        self.schedules = schedules  # name -> current target task definition
        self.calls: list[list[str]] = []
        self.service_updates: dict[str, str] = {}
        self.schedule_updates: dict[str, str] = {}

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        key = (args[0], args[1])
        if key == ("cloudformation", "describe-stack-resources"):
            return json.dumps(list(_STACK.values()))
        if key == ("ecs", "list-services"):
            return "\n".join(
                f"arn:aws:ecs:ca-central-1:1:service/luciel-dev/{n}" for n in self.services
            )
        if key == ("ecs", "describe-services"):
            return json.dumps([{"name": n, "td": td} for n, td in self.services.items()])
        if key == ("ecs", "update-service"):
            self.service_updates[args[args.index("--service") + 1]] = args[
                args.index("--task-definition") + 1
            ]
            return "PRIMARY\n"
        if key == ("ecs", "wait"):
            return ""
        if key == ("scheduler", "list-schedules"):
            prefix = args[args.index("--name-prefix") + 1]
            return json.dumps([n for n in self.schedules if n.startswith(prefix)])
        if key == ("scheduler", "get-schedule"):
            name = args[args.index("--name") + 1]
            return json.dumps(
                {
                    "Arn": f"arn:aws:scheduler:ca-central-1:1:schedule/default/{name}",
                    "CreationDate": "2026-09-01T00:00:00Z",
                    "LastModificationDate": "2026-10-19T04:30:00Z",
                    "Name": name,
                    "GroupName": "default",
                    "ScheduleExpression": "cron(55 * * * ? *)",
                    "FlexibleTimeWindow": {"Mode": "OFF"},
                    "State": "ENABLED",
                    "Target": {
                        "Arn": "arn:aws:ecs:ca-central-1:1:cluster/luciel-dev",
                        "RoleArn": "arn:aws:iam::1:role/Luciel-dev-Compute-SweepSchedulerRole",
                        "EcsParameters": {
                            "TaskDefinitionArn": self.schedules[name],
                            "LaunchType": "FARGATE",
                        },
                        "Input": json.dumps({"sweep": name.rsplit("-", 1)[-1]}),
                    },
                }
            )
        if key == ("scheduler", "update-schedule"):
            path = args[args.index("--cli-input-json") + 1].removeprefix("file://")
            body = json.loads(Path(path).read_text(encoding="utf-8"))
            assert not {"Arn", "CreationDate", "LastModificationDate"} & set(body)
            self.schedule_updates[body["Name"]] = body["Target"]["EcsParameters"][
                "TaskDefinitionArn"
            ]
            return "{}"
        raise AssertionError(f"unexpected aws call: {args[:3]}")


def test_nothing_moves_when_everything_already_runs_the_stacks_revisions(capsys) -> None:
    aws = _FakeAws(
        services={
            "DataPlane": _STACK["LucieldevComputedataplaneTask7C85D254"],
            "ControlPlane": _STACK["LucieldevComputecontrolplaneTaskE44E4C8B"],
        },
        schedules={"Luciel-dev-sweep-deferred": _STACK["LucieldevComputeSweepTask2F33BE67"]},
    )
    assert (
        reconcile.main(
            ["--env", "dev", "--cluster", "luciel-dev", "--region", "ca-central-1"], aws=aws
        )
        == 0
    )
    assert not aws.service_updates and not aws.schedule_updates
    assert not [c for c in aws.calls if c[:2] == ["ecs", "wait"]]
    assert "already runs the stack's revision" in capsys.readouterr().out


def test_a_service_and_a_schedule_left_out_of_band_by_a_rollback_are_moved_back(capsys) -> None:
    aws = _FakeAws(
        services={
            "DataPlane": f"{_TD}/LucieldevComputedataplaneTask7C85D254:97",  # the rollback's copy
            "ControlPlane": _STACK["LucieldevComputecontrolplaneTaskE44E4C8B"],
        },
        schedules={
            "Luciel-dev-sweep-deferred": f"{_TD}/LucieldevComputeSweepTask2F33BE67:59",
            "Luciel-dev-sweep-retention": _STACK["LucieldevComputeSweepTask2F33BE67"],
            "Luciel-prod-sweep-deferred": f"{_TD}/LucielprodComputeSweepTaskAAAA:3",
        },
    )
    assert (
        reconcile.main(
            ["--env", "dev", "--cluster", "luciel-dev", "--region", "ca-central-1"], aws=aws
        )
        == 0
    )
    assert aws.service_updates == {"DataPlane": _STACK["LucieldevComputedataplaneTask7C85D254"]}
    assert aws.schedule_updates == {
        "Luciel-dev-sweep-deferred": _STACK["LucieldevComputeSweepTask2F33BE67"]
    }
    waits = [c for c in aws.calls if c[:2] == ["ecs", "wait"]]
    assert len(waits) == 1 and "DataPlane" in waits[0] and "ControlPlane" not in waits[0]
    assert "moved 1 service(s) and 1 sweep schedule(s)" in capsys.readouterr().out


def test_a_service_of_a_family_the_stack_does_not_declare_is_a_red_run() -> None:
    aws = _FakeAws(services={"Mystery": f"{_TD}/SomethingElse:1"}, schedules={})
    with pytest.raises(SystemExit, match="does not declare"):
        reconcile.main(
            ["--env", "dev", "--cluster", "luciel-dev", "--region", "ca-central-1"], aws=aws
        )


def test_family_of_reads_the_family_out_of_a_revision_arn() -> None:
    assert (
        reconcile.family_of(f"{_TD}/LucieldevComputeSweepTask2F33BE67:57")
        == "LucieldevComputeSweepTask2F33BE67"
    )
