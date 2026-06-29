-- §10 live verification probe — executed as luciel_app (non-superuser, RLS-bound)
-- against the live RDS via a one-off ECS task. Prints PASS/FAIL NOTICEs; the runner
-- (verify.sh) greps the output and sets the task exit code.
-- Rows: 3 (audit append-only), 4 (no secret material in Postgres; app not superuser),
-- 1 (structural RLS enabled/forced on tenant tables; app not a member of audit owner).

-- ROW 3: audit-log append-only — UPDATE and DELETE must both be rejected.
DO $$
BEGIN
  BEGIN
    UPDATE admin_audit_log SET action = 'TAMPER';
    RAISE WARNING 'ROW3-FAIL: UPDATE on admin_audit_log SUCCEEDED';
  EXCEPTION WHEN others THEN RAISE WARNING 'ROW3-PASS-UPDATE: rejected (%)', SQLERRM; END;
  BEGIN
    DELETE FROM admin_audit_log;
    RAISE WARNING 'ROW3-FAIL: DELETE on admin_audit_log SUCCEEDED';
  EXCEPTION WHEN others THEN RAISE WARNING 'ROW3-PASS-DELETE: rejected (%)', SQLERRM; END;
END $$;

-- ROW 4: no raw-credential columns in instance_connections (pointer-only design).
DO $$
DECLARE c int; ref int;
BEGIN
  SELECT count(*) INTO c FROM information_schema.columns
   WHERE table_name='instance_connections'
     AND column_name IN ('secret','api_key','password','token','access_token','refresh_token','credential','client_secret');
  IF c > 0 THEN RAISE WARNING 'ROW4-FAIL: instance_connections has % raw-credential column(s)', c;
  ELSE RAISE WARNING 'ROW4-PASS: instance_connections has no raw-credential columns'; END IF;
  SELECT count(*) INTO ref FROM information_schema.columns
   WHERE table_name='instance_connections' AND column_name='secret_ref';
  RAISE WARNING 'ROW4-INFO: secret_ref pointer column present = %', (ref=1);
END $$;

-- ROW 4: the app connects as a NON-superuser and is NOT a member of the audit owner.
DO $$
DECLARE su boolean; member boolean;
BEGIN
  SELECT rolsuper INTO su FROM pg_roles WHERE rolname = current_user;
  IF su THEN RAISE WARNING 'ROW4-FAIL: app role % IS superuser', current_user;
  ELSE RAISE WARNING 'ROW4-PASS: app role % is NOT superuser', current_user; END IF;
  SELECT pg_has_role(current_user, 'luciel_audit_owner', 'member') INTO member;
  IF member THEN RAISE WARNING 'ROW3-FAIL: app IS a member of luciel_audit_owner';
  ELSE RAISE WARNING 'ROW3-PASS: app is NOT a member of luciel_audit_owner'; END IF;
END $$;

-- ROW 1 (structural): RLS enabled + forced on the tenant tables.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='public' AND c.relkind='r'
       AND c.relrowsecurity = true
  LOOP
    RAISE WARNING 'ROW1-PASS: % RLS=% forced=%', r.relname, r.rls, r.forced;
  END LOOP;
END $$;
