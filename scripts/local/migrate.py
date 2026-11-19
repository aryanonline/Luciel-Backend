"""Prepare the database for the local Docker stack (the one-shot ``migrate`` service).

The same ordered steps as the README's host recipe and ``scripts/migrate.sh``, minus the
AWS secret: ``migrate.sh`` derives the app role's password from the RDS master secret,
while the local stack keeps the password the container's first start created
(``luciel_dev_pw``), so the host test suite and this stack share one database server.

0. Preflight: with ``LUCIEL_LLM_POSTURE=live`` there must be an AI key. Checked first so
   ``docker compose up`` stops here with one clear sentence instead of a stack trace.
1. ``bootstrap_roles.sql`` as the superuser (roles and grants).
2. ``alembic upgrade head`` as ``luciel_app`` (schema, RLS, grants).
3. ``lock_audit_log.sql`` as the superuser (the append-only audit log).
4. ``verify_audit_lock.py``.
Whatever happens, ``luciel_auth`` is revoked from ``luciel_app`` at the end, as
``migrate.sh`` does. Every step is safe to run again on an existing database.

Environment: ``LUCIEL_SUPERUSER_DSN`` (libpq DSN for the postgres superuser) plus the
app's own settings (``LUCIEL_DATABASE_URL_SYNC`` etc.), which alembic reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

_NO_KEY = """
==============================================================================
 Luciel needs an AI key to answer questions.

 Open the file .env next to docker-compose.yml (copy .env.example if there is
 none) and set at least one of:

     OPENAI_API_KEY=sk-...
     ANTHROPIC_API_KEY=sk-ant-...

 Then run `docker compose up` again. The key stays on your computer: it is
 only passed to the Luciel containers.
==============================================================================
"""


def preflight() -> None:
    if os.environ.get("LUCIEL_LLM_POSTURE", "follow") != "live":
        return
    try:
        secret = json.loads(os.environ.get("LUCIEL_PLATFORM_SECRET") or "{}")
    except json.JSONDecodeError:
        secret = {}
    if not isinstance(secret, dict) or not any(
        str(secret.get(key) or "").strip() for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    ):
        print(_NO_KEY, file=sys.stderr, flush=True)
        raise SystemExit(2)


def run(step: str, *args: str) -> None:
    print(f"[migrate] {step}", flush=True)
    subprocess.run(args, cwd=REPO, check=True)  # noqa: S603 - fixed argv, no shell


def main() -> int:
    preflight()
    superuser = os.environ["LUCIEL_SUPERUSER_DSN"]
    python = sys.executable
    run_sql = str(SCRIPTS / "run_sql.py")
    try:
        run(
            "1/4 bootstrap roles",
            python,
            run_sql,
            "--dsn",
            superuser,
            "--file",
            str(SCRIPTS / "bootstrap_roles.sql"),
        )
        run("2/4 alembic upgrade head (as luciel_app)", python, "-m", "alembic", "upgrade", "head")
        run(
            "3/4 lock the audit log",
            python,
            run_sql,
            "--dsn",
            superuser,
            "--file",
            str(SCRIPTS / "lock_audit_log.sql"),
        )
        run(
            "4/4 verify the audit-log lock",
            python,
            str(SCRIPTS / "verify_audit_lock.py"),
            "--dsn",
            superuser,
        )
    finally:
        # bootstrap_roles.sql lends luciel_app the BYPASSRLS luciel_auth role for the
        # migration only; take it back on every exit path (scripts/migrate.sh does too).
        revoke = str(SCRIPTS / "revoke_migration_role.sql")
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [python, run_sql, "--dsn", superuser, "--file", revoke], cwd=REPO, check=False
        )
    print("[migrate] done: schema, row-level security and the audit-log lock are in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
