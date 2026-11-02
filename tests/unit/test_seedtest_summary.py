"""SeedTest output (round 7 WP-12): one closing SUMMARY line, repeated by the runner.

The harness ends with ``[seedtest] SUMMARY result=... cases=... passed=... failed=...
skipped=... matrix=... seconds=...``; scripts/run-seedtest-task.sh finds that line in the
task's CloudWatch log, prints it again after the task, and writes it (with every
[FAIL]/[SKIP]) into the GitHub job summary when the step runs in Actions.

The last test runs the REAL runner script end to end against a stub ``aws`` on PATH: the
harness flags reach the container override (``--strict`` added), the waiter's 10-minute
give-up is waited past, and the report repeats the SUMMARY and fills the job summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit import seedtest_harness

H = seedtest_harness.load()


def _row(name: str, status: str, kind: str = "case", evidence: str = "e") -> object:
    return H.Row(name, status, evidence, kind)


T3B_SKIP = _row("T3b row1b-llm-cap-live", "skip", evidence="set LUCIEL_SEEDTEST_LIVE_CAP=1 to")
T5_SKIP = _row("T5 row8-primary-to-fallback", "skip", evidence="no OPENAI_API_KEY")


@pytest.mark.parametrize(
    ("rows", "strict", "expected"),
    [
        (
            [_row("T1 a", "pass"), T3B_SKIP],
            True,
            "result=PASS cases=2 passed=1 failed=0 skipped=1 matrix=0 matrix_passed=0 "
            "matrix_failed=0 strict=on unexpected_skips=0",
        ),
        (
            [_row("T1 a", "pass"), _row("M1 x", "pass", "matrix"), _row("M3 y", "fail", "matrix")],
            True,
            "result=FAIL cases=3 passed=2 failed=1 skipped=0 matrix=2 matrix_passed=1 "
            "matrix_failed=1 strict=on unexpected_skips=0",
        ),
        (
            [_row("T1 a", "pass"), T5_SKIP],
            True,
            "result=FAIL cases=2 passed=1 failed=0 skipped=1 matrix=0 matrix_passed=0 "
            "matrix_failed=0 strict=on unexpected_skips=1",
        ),
        (
            [_row("T1 a", "pass"), T5_SKIP],
            False,
            "result=PASS cases=2 passed=1 failed=0 skipped=1 matrix=0 matrix_passed=0 "
            "matrix_failed=0 strict=off unexpected_skips=1",
        ),
        (
            [],
            True,
            "result=PASS cases=0 passed=0 failed=0 skipped=0 matrix=0 matrix_passed=0 "
            "matrix_failed=0 strict=on unexpected_skips=0",
        ),
    ],
)
def test_summary_line(rows: list[object], strict: bool, expected: str) -> None:
    line = H.summary_line(rows, strict=strict, seconds=812.44)
    assert line == f"[seedtest] SUMMARY {expected} seconds=812.4"
    assert "\n" not in line and line.isascii()


def test_the_harness_ends_with_the_summary(capsys: pytest.CaptureFixture[str]) -> None:
    r = H.Results()
    r.record("T1 live-LLM-happy-path", True, "grounded")
    r.record("M4 takeover", True, "ok", kind="matrix", seconds=12.0)
    r.skip("T7 crm-custom-webhook-live", "set LUCIEL_SEEDTEST_WEBHOOK_URL to push")
    assert H.finish(r, strict=True, seconds=61.25) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == (
        "[seedtest] SUMMARY result=PASS cases=3 passed=2 failed=0 skipped=1 matrix=1 "
        "matrix_passed=1 matrix_failed=0 strict=on unexpected_skips=0 seconds=61.2"
    )


def test_the_verdict_line_never_counts_a_skip_as_a_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The local mock run printed "6/8 checks passed" over 3 passes, 3 skips and 2
    failures, right above a SUMMARY saying passed=3: the two lines must agree."""
    r = H.Results()
    for name in ("T2 a", "T3 b", "T4 c"):
        r.record(name, True, "ok")
    for name in ("T1 d", "T6 e"):
        r.record(name, False, "no")
    r.skip("T3b row1b-llm-cap-live", "set LUCIEL_SEEDTEST_LIVE_CAP=1 to")
    r.skip("T5 row8-primary-to-fallback", "no OPENAI_API_KEY")
    r.skip("T7 crm-custom-webhook-live", "set LUCIEL_SEEDTEST_WEBHOOK_URL to push")
    assert r.summary() == "3/8 checks passed, 3 skipped, 2 failed"
    H.finish(r, strict=False, seconds=5.6)
    out = capsys.readouterr().out
    assert "[seedtest] 3/8 checks passed, 3 skipped, 2 failed" in out
    assert "passed=3 failed=2 skipped=3" in out.strip().splitlines()[-1]


# --- the runner's report --------------------------------------------------------- #
SUMMARY = (
    "[seedtest] SUMMARY result=FAIL cases=9 passed=7 failed=1 skipped=1 matrix=8 "
    "matrix_passed=7 matrix_failed=1 strict=on unexpected_skips=0 seconds=640.0"
)
LOG_LINES = [
    "[seedtest] API_BASE=https://api.vantagemind.ai RUN_ID=1 strict=on",
    "[PASS] M1 widget-only+pasted-knowledge (60.1s): grounded_answer=ok[...]",
    "[FAIL] M3 escalation:hot-lead+ungrounded (41.0s): honest_handoff_ack=FAIL[reply=`x`]",
    "[SKIP] T7 crm-custom-webhook-live: set LUCIEL_SEEDTEST_WEBHOOK_URL to push",
    "::error::§10 live-behavior harness found FAIL(s)",
    SUMMARY,
]


def _events(lines: list[str]) -> str:
    return json.dumps({"events": [{"message": line + "\n"} for line in lines]})


def _report(stdin: str, tmp_path: Path, *, code: str = "1") -> tuple[str, str]:
    step_summary = tmp_path / "step-summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(step_summary), "PYTHONUTF8": "1"}
    out = subprocess.run(  # noqa: S603 — the interpreter running this test, fixed argv
        [sys.executable, "-c", seedtest_harness.embedded_python("PY_REPORT"), code, "dev"],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=env,
    ).stdout
    return out, step_summary.read_text(encoding="utf-8") if step_summary.exists() else ""


def test_the_report_prints_the_log_then_the_summary_again(tmp_path: Path) -> None:
    out, step = _report(_events(LOG_LINES), tmp_path)
    lines = out.splitlines()
    assert lines[: len(LOG_LINES)] == LOG_LINES
    assert lines[-1] == SUMMARY
    assert "### SeedTest (dev)" in step and f"`{SUMMARY}`" in step
    assert "Task exit code: `1`" in step
    assert "- [FAIL] M3 escalation:hot-lead+ungrounded" in step
    assert "reply='x'" in step  # backticks cannot break the markdown
    assert "- [SKIP] T7 crm-custom-webhook-live" in step
    assert "[PASS] M1" not in step


def test_a_run_that_never_finished_says_so(tmp_path: Path) -> None:
    out, step = _report(_events(LOG_LINES[:2]), tmp_path, code="137")
    expected = "[seedtest] SUMMARY missing: the harness did not finish (task exit code 137)"
    assert out.splitlines()[-1] == expected
    assert expected in step


@pytest.mark.parametrize("stdin", ["", "not json", '{"events": []}'])
def test_no_logs_yet(stdin: str, tmp_path: Path) -> None:
    out, _ = _report(stdin, tmp_path)
    assert "(no harness output in CloudWatch for this task yet)" in out
    assert out.splitlines()[-1].startswith("[seedtest] SUMMARY missing")


def test_without_actions_there_is_no_step_summary(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", seedtest_harness.embedded_python("PY_REPORT"), "0", "dev"],
        input=_events(LOG_LINES),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**env, "PYTHONUTF8": "1"},
    ).stdout
    assert out.splitlines()[-1] == SUMMARY


# --- the real runner, end to end against a stub aws ------------------------------ #
FAKE_AWS = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_STATE/calls.log"
case "$1 $2" in
  "cloudformation list-exports")
    case "$*" in
      *ClusterName*) echo "luciel-cluster" ;;
      *) echo "arn:aws:ecs:ca-central-1:1:task-definition/seedtest:9" ;;
    esac ;;
  "ecs list-services") echo "arn:aws:ecs:ca-central-1:1:service/luciel-cluster/data" ;;
  "ecs describe-services") echo '{"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}' ;;
  "ecs run-task")
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--overrides" ]; then printf '%s' "$2" > "$FAKE_STATE/overrides.json"; fi
      shift
    done
    echo "arn:aws:ecs:ca-central-1:1:task/luciel-cluster/abc123" ;;
  "ecs wait")
    n=$(cat "$FAKE_STATE/waits" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$FAKE_STATE/waits"
    [ "$n" -ge 2 ] ;;
  "ecs describe-tasks") echo "$FAKE_EXIT_CODE" ;;
  "logs filter-log-events")
    reads=$(cat "$FAKE_STATE/log_reads" 2>/dev/null || echo 0)
    reads=$((reads + 1))
    echo "$reads" > "$FAKE_STATE/log_reads"
    # Only the log group and stream the task definition writes exist (F174: a lookup that
    # asked for anything else silently matched nothing).
    case "$*" in
      *"--log-group-name /luciel/dev/seedtest "*) ;;
      *) echo "An error occurred (ResourceNotFoundException): no such log group" >&2; exit 254 ;;
    esac
    case "$*" in
      *"--log-stream-names luciel-seedtest/SeedTestContainer/abc123 "*) ;;
      *) echo "An error occurred (ResourceNotFoundException): no such log stream" >&2; exit 254 ;;
    esac
    case "${FAKE_LOGS:-ok}" in
      ok) cat "$FAKE_STATE/logs.json" ;;
      denied)
        # What the deploy role got on every 2026-10-25 read: no logs: grant at all.
        msg="An error occurred (AccessDeniedException) when calling the FilterLogEvents"
        msg="$msg operation: User: arn:aws:sts::1:assumed-role/luciel-dev-github-deploy/"
        msg="${msg}GitHubActions is not authorized to perform: logs:FilterLogEvents"
        echo "$msg" >&2
        exit 254 ;;
      late)
        # The stream does not exist for the first two reads, then has the SUMMARY.
        if [ "$reads" -le 2 ]; then
          msg="An error occurred (ResourceNotFoundException) when calling the FilterLogEvents"
          echo "$msg operation: The specified log stream does not exist." >&2
          exit 254
        fi
        cat "$FAKE_STATE/logs.json" ;;
    esac ;;
  "logs get-log-events")
    case "$*" in
      *"--log-group-name /luciel/dev/verify "*) ;;
      *) echo "An error occurred (ResourceNotFoundException): no such log group" >&2; exit 254 ;;
    esac
    case "$*" in
      *"--log-stream-name luciel-verify/VerifyContainer/abc123 "*) ;;
      *) echo "An error occurred (ResourceNotFoundException): no such log stream" >&2; exit 254 ;;
    esac
    case "${FAKE_LOGS:-ok}" in
      ok) echo "[verify] probe output" ;;
      denied)
        echo "An error occurred (AccessDeniedException) when calling GetLogEvents" >&2
        exit 254 ;;
      late)
        echo "An error occurred (ResourceNotFoundException): log stream not there yet" >&2
        exit 254 ;;
    esac ;;
  *) echo "unexpected aws $*" >&2; exit 2 ;;
esac
"""
FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def _runner_available() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False
    probe = subprocess.run(  # noqa: S603
        [bash, "-c", "command -v python3 >/dev/null && python3 -c 'import json'"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


PASS_SUMMARY = (
    "[seedtest] SUMMARY result=PASS cases=8 passed=6 failed=0 skipped=2 matrix=0 "
    "matrix_passed=0 matrix_failed=0 strict=on unexpected_skips=0 seconds=46.3"
)
PASS_LINES = [
    "[seedtest] API_BASE=https://api-dev.vantagemind.ai RUN_ID=2 strict=on",
    "[PASS] T1 live-LLM-happy-path: grounded",
    "[seedtest] §10 live-behavior harness: all checks PASS.",
    PASS_SUMMARY,
]


def _run_runner(
    tmp_path: Path,
    *args: str,
    exit_code: str,
    log_lines: list[str],
    logs: str = "ok",
    wait_seconds: str = "30",
    runner: Path = seedtest_harness.RUNNER,
    waits_already: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    fakebin = tmp_path / "bin"
    state = tmp_path / "state"
    fakebin.mkdir()
    state.mkdir()
    (state / "waits").write_text(str(waits_already), encoding="utf-8")
    for name, body in (("aws", FAKE_AWS), ("sleep", FAKE_SLEEP)):
        script = fakebin / name
        script.write_bytes(body.encode())
        script.chmod(0o755)
    (state / "logs.json").write_text(_events(log_lines), encoding="utf-8")
    step_summary = tmp_path / "step-summary.md"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("LUCIEL_SEEDTEST_")},
        "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_STATE": str(state),
        "FAKE_EXIT_CODE": exit_code,
        "FAKE_LOGS": logs,
        "SEEDTEST_LOG_WAIT_SECONDS": wait_seconds,
        "GITHUB_STEP_SUMMARY": str(step_summary),
        "LUCIEL_SEEDTEST_LIVE_CAP": "1",
        "PYTHONUTF8": "1",
    }
    done = subprocess.run(  # noqa: S603
        [str(shutil.which("bash")), str(runner), "dev", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=120,
    )
    return done, state, step_summary


def _tail(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip()]


def _log_reads(state: Path) -> int:
    return int((state / "log_reads").read_text().strip())


needs_runner = pytest.mark.skipif(not _runner_available(), reason="needs bash and python3 on PATH")


@needs_runner
@pytest.mark.parametrize(
    ("exit_code", "log_lines", "rc"), [("0", PASS_LINES, 0), ("1", LOG_LINES, 1)]
)
def test_the_runner_end_to_end(
    tmp_path: Path, exit_code: str, log_lines: list[str], rc: int
) -> None:
    done, state, step_summary = _run_runner(
        tmp_path, "--matrix", "--only", "M3", exit_code=exit_code, log_lines=log_lines
    )
    out = done.stdout
    assert done.returncode == rc, done.stderr + out
    overrides = json.loads((state / "overrides.json").read_text(encoding="utf-8"))
    container = overrides["containerOverrides"][0]
    assert container["command"] == [
        "python3",
        "scripts/seed_and_livetest.py",
        "--strict",
        "--matrix",
        "--only",
        "M3",
    ]
    assert container["environment"] == [{"name": "LUCIEL_SEEDTEST_LIVE_CAP", "value": "1"}]
    assert (state / "waits").read_text().strip() == "2"  # waited past the waiter's give-up
    assert "still running" in out
    summary = log_lines[-1]
    report = out[out.index("[seedtest] log group:") :]
    assert summary in report.splitlines()
    tail = _tail(report)
    if rc:
        assert tail[-2] == summary and tail[-1].startswith("::error::seedtest task FAILED")
    else:
        assert tail[-2] == summary and "probe PASSED" in tail[-1]
    assert f"`{summary}`" in step_summary.read_text(encoding="utf-8")


def test_the_runner_reads_the_log_group_and_stream_the_task_definition_writes() -> None:
    """An awslogs stream is <streamPrefix>/<container name>/<task id>. Rename either half
    in compute-stack.ts and the strict gate reads nothing and fails a healthy release."""
    stack = (seedtest_harness.ROOT / "infra" / "lib" / "compute-stack.ts").read_text(
        encoding="utf-8"
    )
    found = re.search(
        r"seedTestTask\.addContainer\('(?P<container>\w+)'.*?"
        r"streamPrefix: '(?P<prefix>[\w-]+)'.*?"
        r"logGroup: logGroupFor\('(?P<group>\w+)'\)",
        stack,
        re.DOTALL,
    )
    assert found is not None, "the SeedTest container's log configuration moved"
    assert "logGroupName: `/luciel/${envConfig.envName}/${name}`" in stack
    runner = seedtest_harness.RUNNER.read_text(encoding="utf-8")
    assert f'log_group="/luciel/${{ENVIRONMENT}}/{found["group"]}"' in runner
    assert f'stream="{found["prefix"]}/{found["container"]}/${{taskid}}"' in runner
    # And the fake aws answers only for that pair, for the dev environment and task abc123.
    assert f"--log-group-name /luciel/dev/{found['group']} " in FAKE_AWS
    stream = f"{found['prefix']}/{found['container']}/abc123"
    assert f"--log-stream-names {stream} " in FAKE_AWS


# --- the strict gate reads the SUMMARY or fails (round 7 fact check, 2026-10-25) ---- #
@needs_runner
def test_a_refused_log_read_fails_the_strict_gate_at_once(tmp_path: Path) -> None:
    """Every 2026-10-25 dev deploy: the task exited 0, the deploy role could not read
    /luciel/dev/seedtest, the AccessDenied went to /dev/null, the runner printed
    "SUMMARY missing: the harness did not finish (task exit code 0)" and PASSED."""
    done, state, step_summary = _run_runner(
        tmp_path, "--strict", exit_code="0", log_lines=PASS_LINES, logs="denied"
    )
    out = done.stdout
    assert done.returncode == 1, out
    # The refusal is printed, not swallowed, and nobody waits two minutes for it.
    assert (
        "::error::[seedtest] cannot read the harness log: An error occurred (AccessDeniedException)"
        in out
    )
    assert _log_reads(state) == 1
    tail = _tail(out)
    assert tail[-2] == (
        "[seedtest] SUMMARY missing: CloudWatch refused the read of /luciel/dev/seedtest "
        "(task exit code 0)"
    )
    assert tail[-1].startswith("::error::seedtest strict gate FAILED: the task exited 0")
    assert "probe PASSED" not in out
    assert "SUMMARY missing: CloudWatch refused the read" in step_summary.read_text(
        encoding="utf-8"
    )


@needs_runner
def test_the_strict_gate_is_the_default(tmp_path: Path) -> None:
    done, _, _ = _run_runner(tmp_path, exit_code="0", log_lines=PASS_LINES, logs="denied")
    assert done.returncode == 1
    assert "strict gate FAILED" in done.stdout


@needs_runner
def test_no_strict_falls_back_to_the_exit_code_and_says_so(tmp_path: Path) -> None:
    done, _, _ = _run_runner(
        tmp_path, "--no-strict", exit_code="0", log_lines=PASS_LINES, logs="denied"
    )
    out = done.stdout
    assert done.returncode == 0, out
    assert "::warning::seedtest --no-strict: no SUMMARY line was read" in out
    assert _tail(out)[-1] == "[seedtest] §10 live-behavior probe PASSED."


@needs_runner
def test_a_stream_that_appears_late_is_waited_for(tmp_path: Path) -> None:
    done, state, _ = _run_runner(tmp_path, exit_code="0", log_lines=PASS_LINES, logs="late")
    out = done.stdout
    assert done.returncode == 0, out
    assert _log_reads(state) == 3  # two ResourceNotFound reads, then the SUMMARY
    tail = _tail(out)
    assert tail[-2] == PASS_SUMMARY and "probe PASSED" in tail[-1]


@needs_runner
def test_a_stream_with_no_summary_by_the_deadline_fails_the_strict_gate(tmp_path: Path) -> None:
    done, state, _ = _run_runner(
        # sleep is faked, so the 5 s deadline is a handful of fast re-reads; 1 s was
        # shorter than one read on a slow Windows process spawn (a read-once flake).
        tmp_path,
        exit_code="0",
        log_lines=PASS_LINES[:2],
        wait_seconds="5",
    )
    out = done.stdout
    assert done.returncode == 1, out
    assert _log_reads(state) >= 2  # retried until the deadline, not read once
    tail = _tail(out)
    assert tail[-2].startswith("[seedtest] SUMMARY missing: no SUMMARY line in the task's stream")
    assert tail[-1].startswith("::error::seedtest strict gate FAILED")


@needs_runner
@pytest.mark.parametrize("mode", ["--strict", "--no-strict"])
def test_a_summary_that_is_not_a_pass_fails_even_on_exit_zero(tmp_path: Path, mode: str) -> None:
    done, _, _ = _run_runner(tmp_path, mode, exit_code="0", log_lines=LOG_LINES)
    assert done.returncode == 1, done.stdout
    assert _tail(done.stdout)[-1] == (
        "::error::seedtest gate FAILED: the task exited 0 but the harness SUMMARY is not a "
        f"PASS: {SUMMARY}"
    )


# --- the §10 verify runner keeps its log read's error; its verdict is the exit code --- #
VERIFY_RUNNER = seedtest_harness.ROOT / "scripts" / "run-verify-task.sh"


@needs_runner
@pytest.mark.parametrize(
    ("logs", "exit_code", "rc", "expected"),
    [
        ("ok", "0", 0, "[verify] probe output"),
        ("late", "0", 0, "(logs not yet available for /luciel/dev/verify)"),
        (
            "denied",
            "0",
            0,
            "::warning::[verify] cannot read the probe log: An error occurred "
            "(AccessDeniedException) when calling GetLogEvents",
        ),
        (
            "denied",
            "1",
            1,
            "::warning::[verify] cannot read the probe log: An error occurred "
            "(AccessDeniedException) when calling GetLogEvents",
        ),
        ("ok", "1", 1, "[verify] probe output"),
    ],
)
def test_the_verify_runner_says_why_it_has_no_log_and_gates_on_the_exit_code(
    tmp_path: Path, logs: str, exit_code: str, rc: int, expected: str
) -> None:
    done, _, _ = _run_runner(
        tmp_path,
        exit_code=exit_code,
        log_lines=[],
        logs=logs,
        runner=VERIFY_RUNNER,
        waits_already=1,  # the fake's first wait is the waiter's give-up; verify has no retry
    )
    out = done.stdout
    assert done.returncode == rc, done.stderr + out
    assert expected in [line.rstrip() for line in out.splitlines()]
    tail = _tail(out)[-1]
    if rc:
        assert tail == f"::error::verify task FAILED (exit {exit_code})"
    else:
        assert tail == "[verify] §10 live probe PASSED."
    if logs != "denied":
        assert "::warning::" not in out
