#!/usr/bin/env python3
"""Debian security-tracker evidence for the image CVE review (round 7 WP-9).

The monthly steward routine renews or retires waivers in .security/cve-allowlist.json
from the "Dependency & CVE review YYYY-MM" issue, but its cloud sandbox cannot reach
security-tracker.debian.org (egress-blocked; found 2026-10-04), so it was renewing on
search-engine evidence. GitHub's runners CAN reach the tracker, so the workflows that
post on that issue (image-rebuild.yml after the gate, dependency-review.yml) run this
and append a compact table: for every waived CVE and every CVE the gate reports as
`not allowlisted`, the tracker's per-release version and status, plus the no-dsa /
postponed note and the fixed-in version, with the UTC time it was fetched.

    python3 scripts/debian_tracker_status.py --allowlist .security/cve-allowlist.json \\
        --gate-output gate.txt [--cve CVE-...] [--releases trixie,forky,sid] \\
        [--markdown tracker.md]

Stdlib only (runs on the runner's python3 before any venv exists). It NEVER fails
the job: a fetch error prints `tracker unreachable` for that CVE and moves on, and
the exit code is always 0 — evidence gathering must not turn into an outage.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

TRACKER = "https://security-tracker.debian.org/tracker/"
# The image is python:3.12.x-slim = Debian 13 "trixie"; forky/sid show where a fix
# lands first. bookworm rows are noise for this image and are left out by default.
DEFAULT_RELEASES = ("trixie", "forky", "sid")
USER_AGENT = "luciel-cve-review/1 (GitHub Actions runner; image CVE waiver evidence)"

_CVE = re.compile(r"CVE-\d{4}-\d{4,}")
# check_image_scan.py: `::error::CVE-2026-95619 (gcc-14 14.2.0-19): HIGH and not allowlisted`
# (older gate output has no parenthesised package: `CVE-...: HIGH and not allowlisted`).
_NOT_ALLOWLISTED = re.compile(
    r"(CVE-\d{4}-\d{4,})(?:\s+\(([^)]*)\))?:\s*([A-Za-z]+)\s+and not allowlisted"
)
# Notes block: `[trixie] - glibc <no-dsa> (Minor issue)`
_NOTE = re.compile(r"^\[(\S+)\]\s+-\s+(\S+)\s+<([^>]+)>\s*(.*)$")


@dataclass
class Target:
    cve: str
    why: str
    package: str | None  # Debian source package, when the waiver or the gate names it


@dataclass
class Row:
    package: str
    release: str
    version: str
    status: str
    note: str


class _TrackerPage(HTMLParser):
    """Collects every table (rows of cell text), the <pre> notes, and the <h1>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.notes: list[str] = []
        self.h1 = ""
        self._cell: list[str] | None = None
        self._in_pre = False
        self._in_h1 = False
        self._pre: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.tables[-1].append([])
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "pre":
            self._in_pre, self._pre = True, []
        elif tag == "br" and self._in_pre:
            self._pre.append("\n")
        elif tag == "h1":
            self._in_h1 = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self.tables and self.tables[-1]:
                self.tables[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "pre" and self._in_pre:
            self._in_pre = False
            self.notes += [line.strip() for line in "".join(self._pre).splitlines()]
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._in_pre:
            self._pre.append(data)
        if self._in_h1:
            self.h1 += data


def _table(page: _TrackerPage, header: tuple[str, ...]) -> list[list[str]]:
    for table in page.tables:
        if table and tuple(table[0][: len(header)]) == header:
            return table[1:]
    return []


def _releases_of(cell: str) -> list[str]:
    # "forky, sid" / "bookworm (security)" / "(unstable)" -> ["forky", "sid"] / ...
    names = []
    for part in cell.split(","):
        name = part.strip().removesuffix("(security)").strip().strip("()")
        names.append("sid" if name == "unstable" else name)
    return [n for n in names if n]


def parse_tracker(html: str, package: str | None, releases: tuple[str, ...]) -> list[Row]:
    """Rows of the tracker's source-package table for `releases` (and `package`)."""
    page = _TrackerPage()
    page.feed(html)
    status_rows = _table(page, ("Source Package", "Release", "Version", "Status"))
    fixed_rows = _table(page, ("Package", "Type", "Release", "Fixed Version"))

    # (package, release) -> "fixed in X (DSA-…)" / "unfixed (bug #N)"
    fixed: dict[tuple[str, str], str] = {}
    for cells in fixed_rows:
        if len(cells) < 4:
            continue
        pkg, _type, rel, version = cells[:4]
        origin = cells[5] if len(cells) > 5 else ""
        bug = cells[6] if len(cells) > 6 else ""
        if "unfixed" in version:
            text = "unfixed" + (f" (bug #{bug})" if bug else "")
        elif "not affected" in version:
            text = "not affected"
        else:
            text = f"fixed in {version}" + (f" ({origin})" if origin else "")
        for name in _releases_of(rel):
            fixed[(pkg, name)] = text

    # (package, release) -> "no-dsa (Minor issue)"
    tagged: dict[tuple[str, str], str] = {}
    for line in page.notes:
        m = _NOTE.match(line)
        if m:
            tagged[(m[2], m[1])] = f"{m[3]} {m[4]}".strip()

    rows: list[Row] = []
    current = ""
    for cells in status_rows:
        if len(cells) < 4:
            continue
        current = cells[0].removesuffix("(PTS)").strip() or current
        names = _releases_of(cells[1])
        if not any(name in releases for name in names):
            continue
        notes = []
        for name in names:
            if (current, name) in tagged:
                notes.append(f"{name}: {tagged[(current, name)]}")
            if (current, name) in fixed:
                notes.append(f"{name}: {fixed[(current, name)]}")
        rows.append(Row(current, cells[1], cells[2], cells[3], "; ".join(notes)))
    if package and any(r.package == package for r in rows):
        rows = [r for r in rows if r.package == package]
    return rows


def targets_from(allowlist: Path | None, gate_outputs: list[Path], cves: list[str]) -> list[Target]:
    found: dict[str, Target] = {}
    if allowlist is not None:
        try:
            data = json.loads(allowlist.read_text(encoding="utf-8"))
            for entry in data.get("allow", []):
                if isinstance(entry, dict) and _CVE.fullmatch(str(entry.get("cve", ""))):
                    cve = str(entry["cve"])
                    why = f"waived until {entry.get('expires', '?')}"
                    found.setdefault(cve, Target(cve, why, entry.get("package") or None))
        except (OSError, ValueError, AttributeError) as exc:
            print(f"(allowlist unreadable: {type(exc).__name__})", file=sys.stderr)
    for path in gate_outputs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _NOT_ALLOWLISTED.finditer(text):
            cve, installed, severity = m[1], (m[2] or "").split(), m[3].upper()
            if cve in found:
                continue
            why = f"not allowlisted ({severity}"
            why += f", installed {installed[1]})" if len(installed) > 1 else ")"
            found[cve] = Target(cve, why, installed[0] if installed else None)
    for cve in cves:
        found.setdefault(cve, Target(cve, "requested", None))
    return list(found.values())


def fetch(cve: str, timeout: float) -> tuple[str, str | None]:
    """(outcome, html): outcome is "ok", "not in tracker" or "tracker unreachable"."""
    if not _CVE.fullmatch(cve):
        return "not a CVE id", None
    # The URL is the fixed https tracker prefix plus a validated CVE id.
    request = urllib.request.Request(TRACKER + cve, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            return "ok", response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return ("not in tracker" if exc.code == 404 else "tracker unreachable"), None
    # http.client's IncompleteRead / BadStatusLine / LineTooLong are HTTPException,
    # NOT OSError, and urllib re-raises only OSError as URLError: without this clause
    # one truncated page unwound to main() and replaced the WHOLE table with
    # "(not gathered ...)" (Phase 3 review, 2026-10-19).
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ):
        return "tracker unreachable", None


def _cell(text: str) -> str:
    return text.replace("|", "\\|") or " "


def build_report(
    targets: list[Target],
    releases: tuple[str, ...],
    fetcher: Callable[[str], tuple[str, str | None]],
    now: datetime,
) -> str:
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"#### Debian tracker status (fetched {stamp})",
        "",
        f"From {TRACKER}&lt;CVE&gt; by this GitHub runner; releases shown: {', '.join(releases)}.",
        "",
    ]
    if not targets:
        return "\n".join([*lines, "(no waived or unallowlisted CVEs to look up)"])
    lines += [
        "| CVE | why listed | package | release | version | status | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for target in targets:
        lead = f"| [{target.cve}]({TRACKER}{target.cve}) | {_cell(target.why)} "
        try:
            outcome, html = fetcher(target.cve)
        except Exception as exc:  # one CVE's surprise is one row, never the whole table
            outcome, html = f"tracker unreachable ({type(exc).__name__})", None
        if outcome != "ok" or html is None:
            lines.append(f"{lead}| {_cell(target.package or '')} | | | {outcome} | |")
            continue
        try:
            rows = parse_tracker(html, target.package, releases)
        except Exception as exc:  # parser surprises are evidence gaps, not job failures
            lines.append(f"{lead}| | | | tracker page unparseable ({type(exc).__name__}) | |")
            continue
        if not rows:
            lines.append(
                f"{lead}| {_cell(target.package or '')} | | | "
                f"no {'/'.join(releases)} entry on the tracker | |"
            )
            continue
        for index, row in enumerate(rows):
            # The CVE and why-listed cells only on a CVE's first row: compact to read.
            lines.append(
                f"{lead if index == 0 else '| | '}| {_cell(row.package)} | {_cell(row.release)} "
                f"| {_cell(row.version)} | {_cell(row.status)} | {_cell(row.note)} |"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=None)
    parser.add_argument("--gate-output", type=Path, action="append", default=[])
    parser.add_argument("--cve", action="append", default=[])
    parser.add_argument("--releases", default=",".join(DEFAULT_RELEASES))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)
    releases = tuple(r.strip() for r in args.releases.split(",") if r.strip())
    try:
        targets = targets_from(args.allowlist, args.gate_output, args.cve)
        report = build_report(
            targets, releases, lambda cve: fetch(cve, args.timeout), datetime.now(UTC)
        )
    except Exception as exc:  # never fail the job over evidence gathering
        report = f"#### Debian tracker status\n\n(not gathered: {type(exc).__name__}: {exc})"
    print(report)
    if args.markdown is not None:
        try:
            args.markdown.write_text(report + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"(could not write {args.markdown}: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
