"""The deploy smoke probe's /healthz check — what gates a release and what does not.

``scripts/smoke_probe.sh`` runs after every dev deploy and ROLLS THE SERVICES BACK
when it fails. It compares /healthz's ``degraded`` list to the exact set expected for
the integration mode, so a build that fabricates sends cannot pass.

Round 7 WP-4b added ``escalation_backlog:<n>`` to that list: some tenant's
escalation page is still undelivered after a sweep. That is an operations signal
with its own CloudWatch alarm, not a fault of the build being deployed — gating on
it would roll back a healthy release because one owner's contact is unreachable.
These tests run the probe's own embedded check (extracted from the script, so the
two cannot drift) against the health bodies that matter.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_probe.sh"


def _embedded_check() -> str:
    text = _SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    return text[start : text.index("\nPY\n", start)]


def _run(tmp_path: Path, *, mode: str, posture: str, body: dict[str, object]) -> int:
    healthz = tmp_path / "healthz.json"
    healthz.write_text(json.dumps(body), encoding="utf-8")
    return subprocess.run(  # noqa: S603 — the interpreter running this test, fixed argv
        [sys.executable, "-c", _embedded_check(), mode, posture, str(healthz)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _live(degraded: list[str], status: str = "ok") -> dict[str, object]:
    return {"status": status, "integrationMode": "live", "degraded": degraded}


def test_a_clean_live_build_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, mode="live", posture="test", body=_live([])) == 0


def test_an_undelivered_escalation_backlog_does_not_roll_back_a_healthy_release(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, mode="live", posture="test", body=_live(["escalation_backlog:2"])) == 0


@pytest.mark.parametrize(
    "degraded",
    [
        ["platform_email:NullEmailSender"],
        ["platform_email:NullEmailSender", "escalation_backlog:2"],
        ["stripe_payg_price:unset"],
    ],
)
def test_a_binding_fault_still_fails_the_probe_backlog_or_not(
    tmp_path: Path, degraded: list[str]
) -> None:
    assert _run(tmp_path, mode="live", posture="test", body=_live(degraded)) == 1


def test_a_degraded_database_fails_the_probe(tmp_path: Path) -> None:
    body = _live(["database:unreachable"], status="degraded")
    assert _run(tmp_path, mode="live", posture="test", body=body) == 1


def test_the_wrong_integration_mode_fails_the_probe(tmp_path: Path) -> None:
    body = {"status": "ok", "integrationMode": "mock", "degraded": []}
    assert _run(tmp_path, mode="live", posture="test", body=body) == 1
