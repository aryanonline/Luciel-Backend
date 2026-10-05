"""The Dependabot auto-merge classifier, run exactly as the workflow runs it.

``.github/workflows/dependabot-auto-merge.yml`` embeds a small Python program that
reads a pull-request title and prints the update kind: ``minor`` (merged when CI is
green) or anything else (labelled ``major-update`` and left for a person). It has
been wrong three times in production — a literal backspace byte where ``\\b`` was
meant (nothing merged for a week), a 0.x range bump merged as a minor, and a
patch-level base-image bump labelled a migration — so its decisions are pinned here.
The program is extracted from the workflow file, so the two cannot drift.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "dependabot-auto-merge.yml"
)


def _embedded_program() -> str:
    text = _WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    lines = []
    for line in text[start:].split("\n"):
        if line.strip() == "PY":
            break
        lines.append(line)
    else:  # pragma: no cover — a malformed workflow file
        raise AssertionError("the classifier heredoc is not terminated")
    return textwrap.dedent("\n".join(lines))


def _classify(title: str) -> str:
    done = subprocess.run(  # noqa: S603 — the interpreter running this test, fixed argv
        [sys.executable, "-c", _embedded_program(), title],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_the_workflow_file_carries_no_control_bytes() -> None:
    """A backspace byte (0x08) once stood where a regex word boundary was meant; GitHub
    could not parse the file and no Dependabot PR merged for a week (round 7 WP-0)."""
    raw = _WORKFLOW.read_bytes()
    assert not [b for b in raw if b < 0x20 and b not in (0x09, 0x0A, 0x0D)]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # The grouped upkeep PR.
        ("chore(deps): Bump the minor-and-patch group with 7 updates", "minor"),
        # Ordinary single updates.
        ("chore(deps): Bump boto3 from 1.42.1 to 1.42.9", "minor"),
        ("chore(deps): Bump boto3 from 1.42.1 to 1.43.0", "minor"),
        ("chore(deps-dev): Bump typescript from 5.9.3 to 7.0.2 in /infra", "major"),
        ("chore(deps): Bump actions/checkout from 4 to 7", "major"),
        # 0.x packages carry breaking changes in the second component.
        ("chore(deps-dev): Bump ruff from 0.16.8 to 0.16.9", "minor"),
        ("chore(deps-dev): Bump ruff from 0.16.8 to 0.17.0", "major"),
        # Requirement ranges: the highest bound decides.
        ("Update httpx requirement from <1.4,>=1.3.1 to >=1.3.1,<1.7", "minor"),
        ("Update stripe requirement from <16,>=15 to >=15,<17", "major"),
        ("Update fastapi requirement from <0.139,>=0.138 to >=0.138,<0.142", "major"),
        # The runtime image: a patch of the same line and variant is upkeep ...
        ("chore(deps): Bump python from 3.12.14-slim to 3.12.15-slim", "minor"),
        (
            "build(deps): bump node from 22.23.2-trixie-slim to 22.23.3-trixie-slim in /apps/web",
            "minor",
        ),
        # ... a new minor, a new major, or a new distro is a person's call.
        ("chore(deps): Bump python from 3.12.14-slim to 3.13.1-slim", "runtime"),
        ("chore(deps): Bump python from 3.12-slim to 3.14-slim", "runtime"),
        ("chore(deps): Bump python from 3.12.14-slim to 3.12.15-alpine", "runtime"),
        ("build(deps): bump node from 22.23.2-trixie-slim to 24.1.0-trixie-slim", "runtime"),
        # A package that merely ends in "node" is not the runtime image.
        ("chore(deps-dev): Bump @types/node from 24.13.2 to 26.6.2 in /infra", "major"),
        # Anything unreadable is never merged.
        ("Something Dependabot never wrote", "unknown"),
    ],
)
def test_the_classifier_reads_a_title_the_way_a_careful_person_would(
    title: str, expected: str
) -> None:
    assert _classify(title) == expected


def test_only_a_minor_is_ever_merged() -> None:
    """The merge branch is guarded by ``kind = minor``; ``runtime``, ``major`` and
    ``unknown`` all fall through to the label-and-comment branch."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    merge_at = text.index("gh pr merge")
    guard_at = text.rindex('if [ "$kind" = "minor" ]; then', 0, merge_at)
    # Nothing between the guard and the merge re-opens it for another kind.
    assert "else" not in text[guard_at:merge_at]
