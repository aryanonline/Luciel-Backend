#!/usr/bin/env python3
"""Image CVE gate with a governed allowlist (§5.14).

Replaces a raw count check (`CRITICAL != 0 -> fail`) that had two dishonest edges:

  * It could not represent a REVIEWED exception. Debian ships no fix yet for a
    critical in an Essential base package (perl-base, glibc): the old gate's only
    options were "pipeline red for weeks" or "delete the gate" — and a red pipeline
    also blocked PAUSING an environment, since the pause run rebuilt the image.
  * Its failure paths failed OPEN: `describe-image-scan-findings || echo None`
    turned an API error, an expired scan, or a typo'd repo name into a PASS.

This gate fails CLOSED and makes exceptions explicit, bounded, and reviewed:

  * scan not COMPLETE, unreadable findings, malformed allowlist  -> FAIL
  * any CRITICAL finding not in .security/cve-allowlist.json     -> FAIL
  * a listed CVE still present past its entry's `expires` date   -> FAIL
    (expiry forces a human re-review; renewing = editing the file in a commit)
  * a listed CVE no longer present in the scan                   -> warning only,
    so housekeeping is visible without turning cleanup into an outage.

Usage:
    check_image_scan.py <scan-findings.json> <allowlist.json> [--today YYYY-MM-DD]

<scan-findings.json> is the unmodified output of
`aws ecr describe-image-scan-findings`. `--today` exists for tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

REQUIRED_ENTRY_FIELDS = ("cve", "package", "reason", "added", "expires")


def _fail(message: str) -> int:
    print(f"::error::image-scan gate: {message}")
    return 1


def load_allowlist(raw: dict[str, object]) -> dict[str, dict[str, str]]:
    """Validate shape strictly: a malformed allowlist must fail, not half-apply."""
    entries = raw.get("allow")
    if not isinstance(entries, list):
        raise ValueError("allowlist has no 'allow' list")
    by_cve: dict[str, dict[str, str]] = {}
    for entry in entries:
        missing = [f for f in REQUIRED_ENTRY_FIELDS if not entry.get(f)]
        if missing:
            raise ValueError(f"allowlist entry {entry.get('cve', '?')} missing {missing}")
        date.fromisoformat(entry["added"])  # both must parse or the entry is invalid
        date.fromisoformat(entry["expires"])
        if entry["cve"] in by_cve:
            raise ValueError(f"duplicate allowlist entry {entry['cve']}")
        by_cve[entry["cve"]] = entry
    return by_cve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scan_file")
    parser.add_argument("allowlist_file")
    parser.add_argument("--today", default=None, help="ISO date override (tests only)")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()

    try:
        with open(args.scan_file, encoding="utf-8") as f:
            scan = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read scan findings ({type(exc).__name__}) — failing closed")
    try:
        with open(args.allowlist_file, encoding="utf-8") as f:
            allowlist = load_allowlist(json.load(f))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _fail(f"allowlist invalid ({exc}) — failing closed")

    status = str(scan.get("imageScanStatus", {}).get("status", ""))
    if status != "COMPLETE":
        # FAILED / IN_PROGRESS / missing: an unverified image must not deploy.
        return _fail(f"scan status is {status or 'absent'!r}, not COMPLETE — failing closed")

    findings = scan.get("imageScanFindings", {}).get("findings", []) or []
    criticals = [f for f in findings if str(f.get("severity", "")).upper() == "CRITICAL"]

    failures: list[str] = []
    for finding in criticals:
        cve = str(finding.get("name", ""))
        entry = allowlist.get(cve)
        if entry is None:
            failures.append(f"{cve}: CRITICAL and not allowlisted")
        elif today > date.fromisoformat(entry["expires"]):
            failures.append(
                f"{cve}: allowlist entry expired {entry['expires']} and the CVE is still "
                "present — re-review it and extend 'expires' in a commit, or fix the image"
            )
        else:
            print(f"[gate] {cve} ({entry['package']}) allowlisted until {entry['expires']}")

    present = {str(f.get("name", "")) for f in criticals}
    for cve in allowlist:
        if cve not in present:
            print(f"::warning::allowlist entry {cve} no longer matches any finding — remove it")

    if failures:
        for line in failures:
            print(f"::error::{line}")
        return 1
    print(f"[gate] PASS — {len(criticals)} critical finding(s), all under governed exceptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
