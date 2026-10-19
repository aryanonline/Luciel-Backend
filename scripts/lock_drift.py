#!/usr/bin/env python3
"""Report pyproject range drift: locked versions behind what their range allows.

Round 7 WP-9. With uv.lock committed, an install no longer floats to the newest
version a pyproject range admits — it installs the lock. That is the point (CI tests
what ships), but it means a lock nobody moves goes stale INSIDE its ranges: a patch
release the range already allows never arrives unless Dependabot's weekly `uv` PR
lands. This lists exactly that gap for the monthly "Dependency & CVE review" issue.

The resolution is uv's own, not a per-package guess: the workflow runs

    uv lock --upgrade --dry-run > lock-upgrade.txt 2>&1

which re-resolves every range jointly against the index and prints, without writing
the lock, the moves an upgrade would make (`Update <pkg> v<locked> -> v<newest>`,
plus `Add` / `Remove` for packages that would enter or leave the graph). This script
turns that into a markdown table — direct dependencies (named in pyproject, with
their range) first, transitive ones summarised — and never fails the job.

    python3 scripts/lock_drift.py lock-upgrade.txt [--pyproject pyproject.toml]
        [--markdown drift.md]
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_UPDATE = re.compile(r"^Update\s+(\S+)\s+(.+?)\s+->\s+(.+?)\s*$")
_ADD_REMOVE = re.compile(r"^(Add|Remove)\s+(\S+)\s+(.+?)\s*$")
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*?)\s*$")


def normalize(name: str) -> str:
    """PEP 503 name normalization (uv prints normalized names)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _versions(raw: str) -> str:
    # uv prints `v1.2.3`, or `v1.2.3, v2.0.1` for a package forked across markers.
    return ", ".join(part.strip().removeprefix("v") for part in raw.split(","))


@dataclass(frozen=True)
class Move:
    name: str
    kind: str  # "update" | "add" | "remove"
    locked: str
    newest: str


def parse_moves(text: str) -> list[Move]:
    moves: list[Move] = []
    for line in text.splitlines():
        line = line.strip()
        m = _UPDATE.match(line)
        if m:
            moves.append(Move(normalize(m[1]), "update", _versions(m[2]), _versions(m[3])))
            continue
        m = _ADD_REMOVE.match(line)
        if m:
            version = _versions(m[3])
            if m[1] == "Add":
                moves.append(Move(normalize(m[2]), "add", "—", version))
            else:
                moves.append(Move(normalize(m[2]), "remove", version, "—"))
    return moves


def direct_ranges(pyproject: Path) -> dict[str, str]:
    """Normalized name -> the pyproject range, `(dev)` appended for the dev extra."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    ranges: dict[str, str] = {}
    sections: list[tuple[str, list[str]]] = [("", list(project.get("dependencies", [])))]
    for extra, deps in project.get("optional-dependencies", {}).items():
        sections.append((extra, list(deps)))
    for extra, deps in sections:
        for raw in deps:
            m = _REQUIREMENT.match(raw)
            if not m:
                continue
            spec = m[2] or "(any)"
            ranges.setdefault(normalize(m[1]), f"{spec} ({extra})" if extra else spec)
    return ranges


def _summary(move: Move) -> str:
    if move.kind == "add":
        return f"{move.name} (new) {move.newest}"
    if move.kind == "remove":
        return f"{move.name} {move.locked} (dropped)"
    return f"{move.name} {move.locked} → {move.newest}"


def render(text: str, ranges: dict[str, str]) -> str:
    moves = parse_moves(text)
    if not moves:
        if "No lockfile changes detected" in text or re.search(r"^Resolved \d+", text, re.M):
            return "No drift: every locked version is the newest its pyproject range allows."
        tail = "\n".join(text.strip().splitlines()[-5:]) or "(no output)"
        return f"Drift not computed — `uv lock --upgrade --dry-run` said:\n\n```\n{tail}\n```"
    direct = [m for m in moves if m.name in ranges]
    transitive = [m for m in moves if m.name not in ranges]
    lines = [
        f"{len(direct)} direct and {len(transitive)} transitive package(s) are locked "
        "behind the newest version their ranges allow (uv's joint resolution).",
        "",
    ]
    if direct:
        lines += [
            "| package | locked | newest in range | pyproject range |",
            "|---|---|---|---|",
        ]
        for m in sorted(direct, key=lambda m: m.name):
            lines.append(f"| {m.name} | {m.locked} | {m.newest} | `{ranges[m.name]}` |")
        lines.append("")
    if transitive:
        moved = ", ".join(_summary(m) for m in sorted(transitive, key=lambda m: m.name))
        lines.append(f"Transitive: {moved}")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upgrade_output", type=Path, help="`uv lock --upgrade --dry-run` output")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        text = args.upgrade_output.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        text = f"(could not read {args.upgrade_output}: {type(exc).__name__})"
    try:
        ranges = direct_ranges(args.pyproject)
    except (OSError, ValueError, KeyError) as exc:
        print(f"pyproject unreadable ({type(exc).__name__}); listing all moves as transitive")
        ranges = {}
    report = render(text, ranges)
    print(report)
    if args.markdown is not None:
        args.markdown.write_text(report + "\n", encoding="utf-8")
    return 0  # advisory: drift is information for the review, never a failed job


if __name__ == "__main__":
    sys.exit(main())
