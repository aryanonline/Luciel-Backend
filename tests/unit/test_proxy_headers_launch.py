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
    # Trust is scoped to the VPC CIDR CDK injects (LUCIEL_TRUSTED_PROXY_CIDRS), with
    # "*" only as the no-proxy local fallback. Trusting "*" outright made uvicorn take
    # the LEFTMOST X-Forwarded-For entry — the client-written one — so a spoofed
    # header impersonated any visitor's IP at the per-IP abuse ceilings (audit F047).
    trusted = r"""--forwarded-allow-ips["',= ]+\$\{LUCIEL_TRUSTED_PROXY_CIDRS:-\*\}"""
    assert re.search(trusted, line), (
        f'{name} must pass --forwarded-allow-ips "${{LUCIEL_TRUSTED_PROXY_CIDRS:-*}}" — '
        "the ALB's headers are trusted only from the VPC, never from the open internet"
    )
    assert re.search(r"""--forwarded-allow-ips["',= ]+\*["']""", line) is None, (
        f"{name} trusts X-Forwarded-For from any peer again"
    )


def test_cdk_injects_the_trusted_proxy_cidr_on_the_app_planes():
    """The env var the launch line reads must actually be set by the task definition."""
    stack = (_ROOT / "infra" / "lib" / "compute-stack.ts").read_text(encoding="utf-8")
    assert "LUCIEL_TRUSTED_PROXY_CIDRS: vpc.vpcCidrBlock" in stack


def test_ecs_runs_the_entrypoint_that_was_asserted_above():
    """Pins the assumption that makes the entrypoint the production-critical file."""
    stack = (_ROOT / "infra" / "lib" / "compute-stack.ts").read_text(encoding="utf-8")
    assert "scripts/app-entrypoint.sh" in stack
