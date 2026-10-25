"""The backend installs from a committed lockfile, everywhere (round 7 WP-9).

Before uv.lock, CI and the Dockerfile both ran `uv pip install -e .` against the
pyproject RANGES: every install re-resolved, so a commit CI proved green could deploy
different library versions than it tested (and `psycopg[binary]` / `pydantic[email]`
were installed ad hoc, outside pyproject altogether). These tests pin the contract:

  * uv.lock exists and records exactly pyproject's requirement set (CI's
    `uv lock --check` is the authoritative check; this is the offline mirror);
  * CI and the image install it with `uv sync --frozen` (CI with the dev extra, the
    image without) and nothing installs around it with `uv pip install`;
  * the uv that wrote the lock is the one CI and the image run (one pinned version);
  * Dependabot maintains the lock (`uv` ecosystem), not just the ranges (`pip`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
REVIEW = ROOT / ".github" / "workflows" / "dependency-review.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
DOCKERFILE = ROOT / "Dockerfile"

_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*?)\s*$")


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_spec(spec: str) -> str:
    return ",".join(sorted(part.strip() for part in spec.split(",") if part.strip()))


def _pyproject_requirements() -> set[tuple[str, str, tuple[str, ...], str]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs: set[tuple[str, str, tuple[str, ...], str]] = set()
    groups: list[tuple[str, list[str]]] = [("", project["dependencies"])]
    groups += [(extra, deps) for extra, deps in project["optional-dependencies"].items()]
    for extra, deps in groups:
        for raw in deps:
            m = _REQ.match(raw)
            assert m, f"unparseable requirement {raw!r}"
            name, extras, spec = m.groups()
            extras_t = tuple(sorted(e.strip() for e in (extras or "").split(",") if e.strip()))
            marker = f"extra == '{extra}'" if extra else ""
            reqs.add((_norm_name(name), _norm_spec(spec), extras_t, marker))
    return reqs


def _locked_requirements() -> set[tuple[str, str, tuple[str, ...], str]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root = next(p for p in lock["package"] if p.get("source", {}).get("editable") == ".")
    return {
        (
            _norm_name(r["name"]),
            _norm_spec(r.get("specifier", "")),
            tuple(sorted(r.get("extras", []))),
            r.get("marker", ""),
        )
        for r in root["metadata"]["requires-dist"]
    }


def test_the_lockfile_records_exactly_the_pyproject_requirement_set():
    """A range edited without re-locking would install the OLD resolution silently."""
    assert (ROOT / "uv.lock").is_file(), "uv.lock must be committed"
    assert _locked_requirements() == _pyproject_requirements(), (
        "uv.lock is stale against pyproject.toml - run `uv lock` with the pinned uv"
    )


def test_every_locked_runtime_requirement_resolves_to_one_pinned_version():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions: dict[str, set[str]] = {}
    for package in lock["package"]:
        versions.setdefault(_norm_name(package["name"]), set()).add(package["version"])
    for name, _spec, _extras, marker in _pyproject_requirements():
        assert name in versions, f"{name} is required but not in uv.lock"
        if not marker:
            assert len(versions[name]) == 1, f"{name} is locked to several versions"


def test_the_formerly_ad_hoc_runtime_extras_are_declared_and_locked():
    names = {name for name, *_ in _locked_requirements()}
    assert {"psycopg", "pydantic"} <= names
    lock_names = {
        _norm_name(p["name"])
        for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    }
    assert {"psycopg-binary", "email-validator"} <= lock_names


def test_ci_and_the_image_install_the_frozen_lock_and_nothing_around_it():
    ci = CI.read_text(encoding="utf-8")
    review = REVIEW.read_text(encoding="utf-8")
    docker = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv lock --check" in ci, "CI must fail a pyproject change that was not re-locked"
    assert "uv sync --frozen --extra dev" in ci
    assert "uv sync --frozen --extra dev" in review
    assert "uv sync --frozen --no-dev" in docker
    assert "--extra" not in docker, "the image ships runtime dependencies only"
    for name, text in (("ci.yml", ci), ("dependency-review.yml", review), ("Dockerfile", docker)):
        assert "uv pip install" not in text, f"{name} installs around the lock"
    # The image's interpreter is the base image's own; uv must not download another.
    assert "UV_PYTHON_DOWNLOADS=never" in docker


def test_one_pinned_uv_version_writes_and_installs_the_lock():
    docker = DOCKERFILE.read_text(encoding="utf-8")
    image = re.search(r"COPY --from=ghcr\.io/astral-sh/uv:([0-9][0-9.]*)\s", docker)
    assert image, "the Dockerfile must copy a PINNED uv (not :latest)"
    pinned = image.group(1)
    for workflow in (CI, REVIEW):
        text = workflow.read_text(encoding="utf-8")
        versions = re.findall(
            r"uses: astral-sh/setup-uv@\S+\s+with:\s+(?:#.*\s+)*version: '([^']+)'", text
        )
        assert versions == [pinned], f"{workflow.name}: setup-uv must pin uv {pinned}"


def test_dependabot_maintains_the_lockfile_not_only_the_ranges():
    text = DEPENDABOT.read_text(encoding="utf-8")
    ecosystems = re.findall(r"package-ecosystem:\s*(\S+)", text)
    assert "uv" in ecosystems
    assert "pip" not in ecosystems, "the pip updater leaves uv.lock stale"


def test_fastapis_deliberate_ceiling_is_kept_out_of_the_uv_group():
    """The uv updater folds a pyproject range widening into the weekly group PR, whose
    title the auto-merge classifier honours without its 0.x-is-major rule. fastapi's
    ceiling is a person's call (a 0.137 bump once dropped include_router routes), so
    it is excluded from the group and arrives standalone, classified and labelled
    (Phase 3 review, 2026-10-19)."""
    text = DEPENDABOT.read_text(encoding="utf-8")
    uv_block = text.split("package-ecosystem: uv", 1)[1].split("package-ecosystem:", 1)[0]
    m = re.search(r"exclude-patterns:\s*\[([^\]]*)\]", uv_block)
    assert m, "the uv group must exclude fastapi"
    assert "fastapi" in [p.strip().strip("'\"") for p in m.group(1).split(",")]
