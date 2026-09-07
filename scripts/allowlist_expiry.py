#!/usr/bin/env python3
"""Report governed CVE waivers that expire soon (round 6 WP-B).

The image CVE gate (scripts/check_image_scan.py) fails the DEPLOY the day a waiver
expires; this is the earlier, quieter signal — the monthly dependency review lists
every entry of ``.security/cve-allowlist.json`` and exits non-zero when one expires
within ``--within-days`` (default 14), so the re-review happens before a deploy is
blocked rather than because one was.

    python scripts/allowlist_expiry.py --within-days 14 [--markdown out.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ALLOWLIST = Path(__file__).resolve().parents[1] / ".security" / "cve-allowlist.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--within-days", type=int, default=14)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST)
    args = parser.parse_args()

    data = json.loads(args.allowlist.read_text(encoding="utf-8"))
    entries = [e for e in data.get("entries", data.get("allow", [])) if isinstance(e, dict)]
    today = datetime.now(UTC).date()
    rows: list[tuple[str, str, str, int]] = []
    soon: list[str] = []
    for entry in entries:
        cve = str(entry.get("cve") or "?")
        package = str(entry.get("package") or "?")
        raw = str(entry.get("expires") or "")
        try:
            expires = date.fromisoformat(raw)
        except ValueError:
            rows.append((cve, package, raw or "(none)", -1))
            soon.append(cve)
            continue
        days = (expires - today).days
        rows.append((cve, package, raw, days))
        if days <= args.within_days:
            soon.append(cve)

    lines = ["| CVE | package | expires | days left |", "|---|---|---|---|"]
    for cve, package, raw, days in sorted(rows, key=lambda r: r[3]):
        flag = " ⚠️" if days <= args.within_days else ""
        lines.append(f"| {cve} | {package} | {raw} | {days}{flag} |")
    if not rows:
        lines.append("| (no waivers) | | | |")
    table = "\n".join(lines)
    print(table)
    if args.markdown is not None:
        args.markdown.write_text(table + "\n", encoding="utf-8")
    if soon:
        print(
            f"\n{len(soon)} waiver(s) expire within {args.within_days} days or are malformed: "
            + ", ".join(soon),
            file=sys.stderr,
        )
        return 1
    print(f"\nno waiver expires within {args.within_days} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
