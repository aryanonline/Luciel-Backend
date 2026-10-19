"""SeedTest ``--strict`` (round 7 WP-12): a skip is no longer invisible in the exit code.

Before this, ``Results.skip`` recorded a skip as a pass, so a case that silently stopped
running — a missing key, a precondition that quietly stopped holding — still read "all
checks PASS" and the preprod gate let it through. Under ``--strict`` every skip whose
(case, reason) is outside the explicit owner-gated allowlist fails the run and names the
skipped cases. The allowlist is exactly the two cases the owner gates behind a flag
(LUCIEL_SEEDTEST_LIVE_CAP, LUCIEL_SEEDTEST_WEBHOOK_URL).

The runner (scripts/run-seedtest-task.sh) passes ``--strict`` unless told otherwise; its
embedded override builder is run here verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from tests.unit import seedtest_harness

H = seedtest_harness.load()

T3B = "T3b row1b-llm-cap-live"
T7 = "T7 crm-custom-webhook-live"
T5 = "T5 row8-primary-to-fallback"


def _real_skip_reasons(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The skip reasons the REAL cases print with their gates unset (no network: each
    case returns at its skip before touching a tenant or a provider)."""
    import app.config

    monkeypatch.delenv("LUCIEL_SEEDTEST_LIVE_CAP", raising=False)
    monkeypatch.delenv("LUCIEL_SEEDTEST_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(app.config, "parse_platform_secret", lambda *_a, **_k: {})
    r = H.Results()
    H.test_llm_cap_live(r, uuid.uuid4())
    H.test_crm_custom_webhook_live(r, None)
    H.test_fallback_failover(r)
    return {row.name: row.evidence for row in r.rows if row.status == "skip"}


def test_the_real_owner_gated_skips_are_allowed_and_a_missing_key_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons = _real_skip_reasons(monkeypatch)
    assert set(reasons) == {T3B, T7, T5}
    assert H.is_owner_gated_skip(T3B, reasons[T3B])
    assert H.is_owner_gated_skip(T7, reasons[T7])
    # T5 skipping means the live fallback was never exercised: under strict, a FAIL.
    assert not H.is_owner_gated_skip(T5, reasons[T5])


@pytest.mark.parametrize(
    ("name", "reason", "allowed"),
    [
        (T3B, "set LUCIEL_SEEDTEST_LIVE_CAP=1 to spend 50 real model calls", True),
        (T7, "set LUCIEL_SEEDTEST_WEBHOOK_URL (and optionally ...)", True),
        (T5, "no OPENAI_API_KEY in platform secret (mock mode)", False),
        # The right case with another reason is a case that did not run for a reason
        # nobody gated.
        (T3B, "redis unavailable", False),
        (T7, "LUCIEL_SEEDTEST_WEBHOOK_URL is malformed", False),
        # The flag's words on a different case do not borrow its exemption.
        ("T9 something-new", "set LUCIEL_SEEDTEST_LIVE_CAP=1", False),
        ("M3 escalation", "set LUCIEL_SEEDTEST_WEBHOOK_URL", False),
    ],
)
def test_owner_gated_allowlist(name: str, reason: str, allowed: bool) -> None:
    assert H.is_owner_gated_skip(name, reason) is allowed


def _rows(*specs: tuple[str, str, str]) -> list[object]:
    return [H.Row(name, status, evidence) for name, status, evidence in specs]


GATED = (T3B, "skip", "set LUCIEL_SEEDTEST_LIVE_CAP=1 to spend")
WEBHOOK = (T7, "skip", "set LUCIEL_SEEDTEST_WEBHOOK_URL to push")
MISSING_KEY = (T5, "skip", "no OPENAI_API_KEY in platform secret")
PASSED = ("T1 live-LLM-happy-path", "pass", "grounded")
FAILED = ("T2 row9-free-cap-zero-LLM", "fail", "counted_after_51st=51")


@pytest.mark.parametrize(
    ("rows", "strict", "code"),
    [
        (_rows(PASSED), True, 0),
        (_rows(PASSED, GATED, WEBHOOK), True, 0),
        (_rows(PASSED, GATED, WEBHOOK), False, 0),
        (_rows(PASSED, MISSING_KEY), True, 1),
        # Without --strict a skip stays non-fatal, exactly as before.
        (_rows(PASSED, MISSING_KEY), False, 0),
        (_rows(PASSED, FAILED), False, 1),
        (_rows(PASSED, FAILED, GATED), True, 1),
    ],
)
def test_exit_code(rows: list[object], strict: bool, code: int) -> None:
    assert H.exit_code(rows, strict=strict) == code


def test_strict_failure_names_every_skipped_case(capsys: pytest.CaptureFixture[str]) -> None:
    r = H.Results()
    r.record("T1 live-LLM-happy-path", True, "grounded")
    r.skip(T3B, "set LUCIEL_SEEDTEST_LIVE_CAP=1 to spend 50 real model calls")
    r.skip(T5, "no OPENAI_API_KEY in platform secret (mock mode)")
    r.skip("T6 row11-channel-parity", "provider unreachable")
    code = H.finish(r, strict=True)
    out = capsys.readouterr().out
    assert code == 1
    error = next(line for line in out.splitlines() if line.startswith("::error::--strict"))
    assert "2 case(s)" in error
    assert T5 in error and "T6 row11-channel-parity" in error
    assert T3B not in error  # owner-gated: tolerated, not listed
    assert "all checks PASS" not in out


def test_without_strict_the_same_skips_still_pass(capsys: pytest.CaptureFixture[str]) -> None:
    r = H.Results()
    r.record("T1 live-LLM-happy-path", True, "grounded")
    r.skip(T5, "no OPENAI_API_KEY in platform secret (mock mode)")
    assert H.finish(r, strict=False) == 0
    assert "::error::" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "env", "strict"),
    [
        ([], {}, False),
        (["--strict"], {}, True),
        ([], {"LUCIEL_SEEDTEST_STRICT": "1"}, True),  # the task definition's default
        (["--no-strict"], {"LUCIEL_SEEDTEST_STRICT": "1"}, False),  # the one opt-out
        ([], {"LUCIEL_SEEDTEST_STRICT": "0"}, False),
    ],
)
def test_strict_flag_and_env(argv: list[str], env: dict[str, str], strict: bool) -> None:
    assert H.parse_args(argv, env).strict is strict


# --- the ECS runner ----------------------------------------------------------- #
def _overrides(args: list[str], extra_env: dict[str, str] | None = None) -> dict[str, object]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LUCIEL_SEEDTEST_")}
    env.update(extra_env or {})
    out = subprocess.run(  # noqa: S603 — the interpreter running this test, fixed argv
        [sys.executable, "-c", seedtest_harness.embedded_python("PY_OVERRIDES"), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return json.loads(out)


def _command(args: list[str]) -> list[str]:
    container = _overrides(args)["containerOverrides"][0]  # type: ignore[index]
    assert container["name"] == "SeedTestContainer"
    return list(container["command"])


@pytest.mark.parametrize(
    ("args", "flags"),
    [
        ([], ["--strict"]),
        (["--matrix"], ["--strict", "--matrix"]),
        (["--persist", "e2e-walk"], ["--strict", "--persist", "e2e-walk"]),
        (["--strict", "--matrix"], ["--strict", "--matrix"]),
        (["--no-strict"], ["--no-strict"]),
        (["--no-strict", "--matrix"], ["--no-strict", "--matrix"]),
    ],
)
def test_the_runner_passes_strict_by_default(args: list[str], flags: list[str]) -> None:
    assert _command(args) == ["python3", "scripts/seed_and_livetest.py", *flags]


def test_owner_flags_ride_along_and_nothing_else_does() -> None:
    container = _overrides(
        [],
        {
            "LUCIEL_SEEDTEST_LIVE_CAP": "1",
            "LUCIEL_SEEDTEST_WEBHOOK_URL": "https://hooks.example.test/crm",
            "AWS_SECRET_ACCESS_KEY": "not-for-the-task",
        },
    )["containerOverrides"][0]  # type: ignore[index]
    assert container["environment"] == [
        {"name": "LUCIEL_SEEDTEST_LIVE_CAP", "value": "1"},
        {"name": "LUCIEL_SEEDTEST_WEBHOOK_URL", "value": "https://hooks.example.test/crm"},
    ]


def test_no_owner_flags_means_no_environment_override() -> None:
    assert "environment" not in _overrides([])["containerOverrides"][0]  # type: ignore[index]


def test_the_runner_hands_the_override_to_run_task_and_outwaits_the_waiter() -> None:
    script = seedtest_harness.RUNNER.read_text(encoding="utf-8")
    assert '--overrides "$overrides"' in script
    # `aws ecs wait tasks-stopped` gives up after 10 minutes; a matrix run outlives it.
    assert "until aws ecs wait tasks-stopped" in script
    assert "SEEDTEST_MAX_WAIT_MINUTES" in script


def test_the_deploy_gate_never_opts_out_of_strict() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    )
    runs = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run-seedtest-task.sh" in str(step.get("run", ""))
    ]
    assert runs, "the preprod gate runs the SeedTest task"
    for run in runs:
        for line in run.splitlines():
            if "run-seedtest-task.sh" in line and not line.strip().startswith("#"):
                assert "--no-strict" not in line
