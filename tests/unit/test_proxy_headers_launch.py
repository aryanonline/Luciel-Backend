"""Both uvicorn launch points must pass --proxy-headers (deploy-shape invariant).

The ALB terminates TLS and forwards plain HTTP to the task. Without --proxy-headers
uvicorn ignores X-Forwarded-Proto, so ``request.url`` reconstructs as ``http://...``
while Twilio signs the configured ``https://...`` webhook URL. The HMAC in
``app/channels/signatures.py`` is computed over that URL, so EVERY real inbound SMS
failed verification in dev — a total inbound outage that no unit test could see,
because TestClient never goes through a proxy.

There are two launch points and ECS uses the entrypoint (compute-stack.ts sets
``command: ['bash', 'scripts/app-entrypoint.sh']``), so fixing only the Dockerfile
would have left prod broken. Both are asserted here. String checks are the right
tool for a deploy-shape invariant, same as ``test_sweep_scheduling.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCH_FILES = {
    "Dockerfile": _ROOT / "Dockerfile",
    "scripts/app-entrypoint.sh": _ROOT / "scripts" / "app-entrypoint.sh",
}


def _uvicorn_launch_line(path: Path) -> str:
    line = next(
        (ln for ln in path.read_text(encoding="utf-8").splitlines() if "app.main:app" in ln),
        None,
    )
    assert line, f"{path.name} no longer launches app.main:app"
    return line


@pytest.mark.parametrize("name", sorted(_LAUNCH_FILES))
def test_launch_line_trusts_the_alb_forwarded_proto(name: str):
    line = _uvicorn_launch_line(_LAUNCH_FILES[name])
    assert "--proxy-headers" in line, (
        f"{name} launches uvicorn without --proxy-headers; request.url will "
        "reconstruct as http:// behind the ALB and Twilio signature checks will fail"
    )
    # Separator differs by file: `="*"`/` "*"` in shell, `", "*` in the Dockerfile
    # JSON exec form.
    assert re.search(r"""--forwarded-allow-ips["',= ]+\*""", line), (
        f"{name} sets --proxy-headers without --forwarded-allow-ips; uvicorn only "
        "trusts 127.0.0.1 by default, so the ALB's headers are still ignored"
    )


def test_ecs_runs_the_entrypoint_that_was_asserted_above():
    """Pins the assumption that makes the entrypoint the production-critical file."""
    stack = (_ROOT / "infra" / "lib" / "compute-stack.ts").read_text(encoding="utf-8")
    assert "scripts/app-entrypoint.sh" in stack
