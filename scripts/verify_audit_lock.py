#!/usr/bin/env python3
"""Gate: refuse to report a successful deploy unless the audit-log lock is applied.

``scripts/lock_audit_log.sql`` cannot be a migration, so a database can be at
``alembic upgrade head`` and still be missing it. ``scripts/migrate.sh`` runs this as
its final step to turn that into an immediate, actionable failure instead of an
``undefined_function`` from the nightly sweep days later.

The checks themselves live in ``app.lifecycle.audit_retention`` — beside the sweep
that depends on them, so the sweep entrypoint and the integration suite assert the
same properties this does. This file is only the synchronous, psycopg-over-a-DSN
front end, because the migration task has a DSN and no application config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lifecycle.audit_retention import (  # noqa: E402
    AUDIT_LOCK_CHECKS,
    AUDIT_LOCK_REMEDY,
    lock_failures,
)


def report(results: list[tuple[str, bool]]) -> str:
    lines = [("  ok   " if ok else "  FAIL ") + name for name, ok in results]
    if failed := lock_failures(results):
        lines += ["", f"{len(failed)} audit-lock check(s) failed.", AUDIT_LOCK_REMEDY]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True, help="libpq DSN (any role that can read catalogs)")
    args = ap.parse_args()

    import psycopg

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        results = []
        for check in AUDIT_LOCK_CHECKS:
            cur.execute(check.sql)
            row = cur.fetchone()
            results.append((check.name, bool(row and row[0])))

    print("[verify-audit-lock]\n" + report(results))
    return 1 if lock_failures(results) else 0


if __name__ == "__main__":
    sys.exit(main())
