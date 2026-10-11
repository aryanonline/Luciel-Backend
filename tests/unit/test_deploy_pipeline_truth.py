"""What the deploy pipeline does when the migration fails (round 7 review).

Two deploy-safety properties, read straight from the files the pipeline runs, so a
refactor cannot quietly drop them:

* ``.github/workflows/deploy.yml`` — the migration runs AFTER the Compute stack has
  rolled the services (the migration task lives in that stack and carries the new
  image), so a refused migration leaves the new code serving the old schema. The
  rollback step must fire on that failure, not only on a failed smoke/verify probe.
* ``scripts/migrate.sh`` — bootstrap grants the app role membership in the BYPASSRLS
  role for the migration's ownership transfers; the lock script revokes it on the
  success path. The revoke must also run when alembic fails, or the live app role is
  left able to ``SET ROLE`` past row-level security (audit F085).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _dev_steps() -> list[dict[str, object]]:
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    jobs = doc["jobs"]
    dev = next(j for name, j in jobs.items() if "dev" in name and "steps" in j)
    return list(dev["steps"])


def test_the_dev_deploy_rolls_back_when_the_migration_fails() -> None:
    steps = _dev_steps()
    migrate = next(s for s in steps if "migrations" in str(s.get("name", "")).lower())
    assert migrate.get("id") == "migrate"
    rollback = next(
        s for s in steps if "roll the app services back" in str(s.get("name", "")).lower()
    )
    condition = str(rollback.get("if", ""))
    assert "steps.migrate.outcome == 'failure'" in condition
    assert "steps.smoke.outcome == 'failure'" in condition
    assert "steps.verify.outcome == 'failure'" in condition
    # The migration is still ordered after the Compute deploy and before the probes:
    # the window this rollback exists for.
    names = [str(s.get("name", "")) for s in steps]
    compute = next(i for i, n in enumerate(names) if "Deploy infra stacks" in n)
    assert names.index(str(migrate["name"])) > compute
    assert names.index(str(migrate["name"])) < names.index(str(rollback["name"]))


def test_the_migration_script_revokes_the_migration_role_on_every_exit() -> None:
    script = (ROOT / "scripts" / "migrate.sh").read_text(encoding="utf-8")
    trap_at = script.index("trap revoke_migration_role EXIT")
    # The command line itself, not the header comment that also names it.
    alembic_at = script.index("\nalembic upgrade head")
    assert trap_at < alembic_at, "the trap must be armed before alembic can fail"
    assert re.search(r"revoke_migration_role\(\)\s*\{[^}]*revoke_migration_role\.sql", script)
    revoke = (ROOT / "scripts" / "revoke_migration_role.sql").read_text(encoding="utf-8")
    assert "REVOKE luciel_auth FROM luciel_app" in revoke
    # And the success path still verifies the revoke took (step 4 runs the lock checks).
    assert "verify_audit_lock.py" in script
