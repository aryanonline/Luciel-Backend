# Definition of Done — Luciel Backend PR checklist (Space Instructions §8)

Copy this into the PR description and check each item.

## Source-of-truth fidelity
- [ ] Cited the doc + section for every product-behavior decision.
- [ ] If the task framing conflicted with the documents, the weakest assumption was
      stated in sentence one and the correct path proposed before building.
- [ ] No invented behavior contradicting the documents; silences resolved as the
      minimal honest choice and flagged.
- [ ] Mock not treated as source of truth; if mock vs document disagreed, document won.

## Frontend contract (Space Instructions §7)
- [ ] Paths from http-admin.ts, shapes from schemas/* (camelCase), error codes via the
      transport.ts status map, cookie-carried session — all exact.
- [ ] mock→http swap still requires zero frontend component changes.

## Isolation & security invariants (§3)
- [ ] All three isolation layers present on any new tenant table (RLS policy added;
      app.admin_id set once from the authenticated subject).
- [ ] Secrets in Secrets Manager only — nothing secret in Postgres, logs, audit, export.
- [ ] Audit events emitted for significant actions; hash-chain integrity preserved.
- [ ] External-dependency failures return structured outcomes (never 500s/silent fails).

## CI gate (§5.14/§5.15)
- [ ] Isolation suite + unit + integration + contract + migration apply/rollback +
      ruff + mypy + bandit + detect-secrets + doctrine sync all green.
- [ ] Doctrine-to-code sync: if a doctrine-anchored interface changed, the matching
      Architecture/Vision section was updated in the same PR.
