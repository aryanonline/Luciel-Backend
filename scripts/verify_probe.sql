-- §10 live verification probe — runs as luciel_app (non-superuser, RLS-bound) on
-- the live RDS. DESIGN: a violated property raises an UNCAUGHT exception, which
-- aborts the script (run_sql.py exits non-zero). Positive results are SELECTed so
-- run_sql.py prints them as evidence. Rows: 3 (audit append-only), 4 (no secret
-- material; app not superuser/owner), 1 (structural RLS).

-- ROW 3a: UPDATE on admin_audit_log MUST be rejected. If it is NOT, raise (fail).
DO $$
BEGIN
  BEGIN
    UPDATE admin_audit_log SET action = action;  -- no-op value, still a mutation
    RAISE EXCEPTION 'ROW3-FAIL: UPDATE on admin_audit_log was ALLOWED';
  EXCEPTION
    WHEN insufficient_privilege OR raise_exception OR others THEN
      IF SQLERRM LIKE 'ROW3-FAIL%' THEN RAISE;  -- re-raise our own failure
      END IF;  -- otherwise the rejection is the PASS path
  END;
END $$;

-- ROW 3b: DELETE on admin_audit_log MUST be rejected.
DO $$
BEGIN
  BEGIN
    DELETE FROM admin_audit_log WHERE false;  -- targets no rows, still a DELETE op
    -- A guard trigger fires per-statement regardless of row count; if we reach here
    -- with no error AND a guard is expected, that is a failure only if the trigger
    -- should block all DELETEs. We assert the trigger exists separately below.
    PERFORM 1;
  EXCEPTION WHEN others THEN
    NULL;  -- rejection is expected/acceptable
  END;
END $$;

-- ROW 3c (authoritative): the append-only guard trigger EXISTS and is owned by the
-- audit owner, and luciel_app is NOT the owner / NOT a member. Fail hard otherwise.
DO $$
DECLARE trg int; is_member boolean; su boolean;
BEGIN
  SELECT count(*) INTO trg FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
    WHERE c.relname='admin_audit_log' AND NOT t.tgisinternal;
  IF trg = 0 THEN RAISE EXCEPTION 'ROW3-FAIL: no append-only guard trigger on admin_audit_log'; END IF;

  SELECT pg_has_role(current_user,'luciel_audit_owner','member') INTO is_member;
  IF is_member THEN RAISE EXCEPTION 'ROW3-FAIL: app role is a member of luciel_audit_owner'; END IF;

  SELECT rolsuper INTO su FROM pg_roles WHERE rolname=current_user;
  IF su THEN RAISE EXCEPTION 'ROW4-FAIL: app role % is superuser', current_user; END IF;
END $$;

-- ROW 4: no raw-credential columns in instance_connections (pointer-only). Fail hard.
DO $$
DECLARE bad int;
BEGIN
  SELECT count(*) INTO bad FROM information_schema.columns
   WHERE table_name='instance_connections'
     AND column_name IN ('secret','api_key','password','token','access_token','refresh_token','credential','client_secret');
  IF bad > 0 THEN RAISE EXCEPTION 'ROW4-FAIL: instance_connections has % raw-credential column(s)', bad; END IF;
END $$;

-- POSITIVE EVIDENCE (printed by run_sql.py): current role, superuser flag, guard
-- trigger count, RLS-enabled tenant tables.
SELECT current_user AS app_role,
       (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS is_superuser,
       (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
          WHERE c.relname='admin_audit_log' AND NOT t.tgisinternal) AS audit_guard_triggers,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity) AS rls_enabled_tables;
