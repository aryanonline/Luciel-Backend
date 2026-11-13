#!/usr/bin/env python3
"""Execute a .sql file via psycopg (no psql CLI -> no perl -> no CVE-2026-12087).

Handles the small subset of psql meta-syntax our bootstrap uses:
  - `\\if :{?app_password}` / `\\endif`  -> include block only if --app-password given
  - `:'app_password'`                     -> quoted-literal substitution
Everything else is plain SQL executed as one script (autocommit, ON_ERROR_STOP).

Usage:
  run_sql.py --dsn <libpq-dsn> --file scripts/bootstrap_roles.sql [--app-password PW]
"""

from __future__ import annotations

import argparse
import sys

import psycopg


def render(sql: str, app_password: str | None) -> str:
    lines = sql.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("\\if :{?app_password}"):
            skipping = app_password is None  # skip the block if no password provided
            continue
        if stripped.startswith("\\endif"):
            skipping = False
            continue
        if skipping:
            continue
        out.append(line)
    rendered = "\n".join(out)
    if app_password is not None:
        # psql :'var' -> a safely single-quoted literal
        literal = "'" + app_password.replace("'", "''") + "'"
        rendered = rendered.replace(":'app_password'", literal)
    return rendered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--app-password", default=None)
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        sql = render(fh.read(), args.app_password)

    # autocommit so role/DDL statements behave like psql's default script mode.
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            # Surface NOTICE/WARNING messages and any final result rows as evidence
            # (used by the §10 verify probe). psycopg exposes notices on the conn.
            for note in getattr(conn, "notices", []) or []:
                print(f"[run_sql:notice] {str(note).rstrip()}")
            try:
                if cur.description is not None:
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchall()
                    print(f"[run_sql:result] columns={cols}")
                    for r in rows:
                        print(f"[run_sql:result] {r}")
            except psycopg.ProgrammingError:
                pass  # last statement returned no result set
    print(f"[run_sql] applied {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
