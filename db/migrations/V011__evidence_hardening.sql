-- Evidence hardening (council audit finding F-01, with F-07/F-22 foundation parts).
-- The same file is included in all four projects under each project's next free version number.
-- It only depends on the shared foundation (V001-V006) and is safe where Project 1's V008 already ran.

-- 1. Who really made a change: keep the client-supplied app.user label, but also store the login.
-- Rows written before this migration keep NULL: their login was never recorded.
ALTER TABLE audit.change_log ADD COLUMN IF NOT EXISTS login_user text;
ALTER TABLE audit.change_log ALTER COLUMN login_user SET DEFAULT session_user;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'change_log_login_user_recorded') THEN
    ALTER TABLE audit.change_log ADD CONSTRAINT change_log_login_user_recorded
      CHECK (login_user IS NOT NULL) NOT VALID;
  END IF;
END $$;

-- 2. Audit function: owner rights are needed so restricted roles can write the log, but it must
--    not be attachable to arbitrary (temporary) tables to forge or pollute entries.
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
  src jsonb;
  pk  jsonb := '{}'::jsonb;
  i   int;
BEGIN
  IF TG_TABLE_SCHEMA NOT IN ('ref','lab','trace','hist','spec') THEN
    RAISE EXCEPTION 'audit.log_change() is only allowed on project tables, not %.%', TG_TABLE_SCHEMA, TG_TABLE_NAME;
  END IF;
  src := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
  FOR i IN 0 .. TG_NARGS - 1 LOOP
    pk := pk || jsonb_build_object(TG_ARGV[i], src -> TG_ARGV[i]);
  END LOOP;
  IF TG_OP = 'UPDATE' AND to_jsonb(OLD) = to_jsonb(NEW) THEN
    RETURN NULL;
  END IF;
  INSERT INTO audit.change_log (table_name, operation, row_pk, old_row, new_row)
  VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, pk,
          CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END);
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION audit.log_change() FROM PUBLIC;

-- 3. Analysis timeline: maintained by the database with owner rights, so inserting roles need
--    no UPDATE privilege; refuses to run on anything except lab.analysis; serialises per tank.
CREATE OR REPLACE FUNCTION lab.set_analysis_validity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, lab AS $$
DECLARE next_sample timestamptz;
BEGIN
  IF TG_TABLE_SCHEMA <> 'lab' OR TG_TABLE_NAME <> 'analysis' OR TG_WHEN <> 'BEFORE' OR TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'lab.set_analysis_validity() may only run as the BEFORE INSERT trigger of lab.analysis';
  END IF;
  PERFORM 1 FROM ref.tank WHERE tank_id = NEW.tank_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM lab.analysis WHERE tank_id = NEW.tank_id AND sampled_at = NEW.sampled_at) THEN
    NEW.valid_during := tstzrange(NEW.sampled_at, NULL, '[)');
    RETURN NEW;
  END IF;
  SELECT min(sampled_at) INTO next_sample FROM lab.analysis
    WHERE tank_id = NEW.tank_id AND sampled_at > NEW.sampled_at;
  NEW.valid_during := tstzrange(NEW.sampled_at, next_sample, '[)');
  UPDATE lab.analysis SET valid_during = tstzrange(lower(valid_during), NEW.sampled_at, '[)')
    WHERE tank_id = NEW.tank_id AND valid_during @> NEW.sampled_at;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION lab.set_analysis_validity() FROM PUBLIC;

-- Keys are immutable and validity is only changed by the timeline triggers themselves
-- (pg_trigger_depth() > 1). Corrections to values belong in lab.analysis_result.
CREATE OR REPLACE FUNCTION lab.guard_analysis_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.tank_id, NEW.sampled_at) IS DISTINCT FROM (OLD.tank_id, OLD.sampled_at) THEN
    RAISE EXCEPTION 'Analysis tank and sampling time are immutable';
  END IF;
  IF NEW.valid_during IS DISTINCT FROM OLD.valid_during AND pg_trigger_depth() < 2 THEN
    RAISE EXCEPTION 'Analysis validity is maintained by the database; insert a new analysis instead';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS analysis_key_guard ON lab.analysis;
CREATE TRIGGER analysis_key_guard BEFORE UPDATE ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION lab.guard_analysis_key();

-- A deleted analysis hands its period back to the previous remaining sample of the same tank,
-- so the timeline stays gap-free exactly as if the deleted sample had never been entered (audited below).
CREATE OR REPLACE FUNCTION lab.close_gap_after_delete() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, lab AS $$
BEGIN
  IF TG_TABLE_SCHEMA <> 'lab' OR TG_TABLE_NAME <> 'analysis' THEN
    RAISE EXCEPTION 'lab.close_gap_after_delete() may only run on lab.analysis';
  END IF;
  -- Rebuild from the samples that remain, so deleting several analyses in one statement
  -- gives the same result in any row order.
  UPDATE lab.analysis p
     SET valid_during = tstzrange(p.sampled_at,
           (SELECT min(n.sampled_at) FROM lab.analysis n
             WHERE n.tank_id = p.tank_id AND n.sampled_at > p.sampled_at), '[)')
   WHERE p.tank_id = OLD.tank_id
     AND p.sampled_at = (SELECT max(q.sampled_at) FROM lab.analysis q
                          WHERE q.tank_id = OLD.tank_id AND q.sampled_at < OLD.sampled_at);
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lab.close_gap_after_delete() FROM PUBLIC;
DROP TRIGGER IF EXISTS analysis_close_gap ON lab.analysis;
CREATE TRIGGER analysis_close_gap AFTER DELETE ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION lab.close_gap_after_delete();

-- 4. Limits are versioned evidence: a version may only be closed (finite upper bound set once),
--    never edited or deleted. A changed limit is a new version. Application roles (quality) may
--    not start or close a version in the past, because that would silently re-judge analyses that
--    were already evaluated; loaders (ingest role, table owner, superuser) may write history.
CREATE OR REPLACE FUNCTION lab.guard_parameter_limit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  today date := (now() AT TIME ZONE 'Europe/Berlin')::date;
  may_write_history boolean :=
       pg_has_role(current_user, 'role_ingest', 'MEMBER')
    OR pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'MEMBER')
    OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user);
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Parameter limits are history; close the version instead of deleting it';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NOT may_write_history AND lower(NEW.valid_during) < today THEN
      RAISE EXCEPTION 'A new limit version cannot start before today (%)', today;
    END IF;
    RETURN NEW;
  END IF;
  IF (NEW.process_code, NEW.parameter_code, NEW.lsl, NEW.target, NEW.usl, NEW.source, lower(NEW.valid_during))
     IS DISTINCT FROM (OLD.process_code, OLD.parameter_code, OLD.lsl, OLD.target, OLD.usl, OLD.source, lower(OLD.valid_during))
     OR NOT upper_inf(OLD.valid_during) OR upper_inf(NEW.valid_during) THEN
    RAISE EXCEPTION 'A parameter limit version can only be closed; insert a new version for changed limits';
  END IF;
  IF NOT may_write_history AND upper(NEW.valid_during) < today THEN
    RAISE EXCEPTION 'A limit version cannot be closed before today (%)', today;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS parameter_limit_guard ON lab.parameter_limit;
CREATE TRIGGER parameter_limit_guard BEFORE INSERT OR UPDATE OR DELETE ON lab.parameter_limit
  FOR EACH ROW EXECUTE FUNCTION lab.guard_parameter_limit();

-- 5. Audit the evidence that tracing and release decisions depend on.
DROP TRIGGER IF EXISTS analysis_audit ON lab.analysis;
CREATE TRIGGER analysis_audit AFTER INSERT OR UPDATE OR DELETE ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('analysis_id');
DROP TRIGGER IF EXISTS parameter_limit_audit ON lab.parameter_limit;
CREATE TRIGGER parameter_limit_audit AFTER INSERT OR UPDATE OR DELETE ON lab.parameter_limit
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('limit_id');
DROP TRIGGER IF EXISTS load_audit ON trace.load;
CREATE TRIGGER load_audit AFTER INSERT OR UPDATE OR DELETE ON trace.load
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('load_id');
DROP TRIGGER IF EXISTS load_lot_audit ON trace.load_lot;
CREATE TRIGGER load_lot_audit AFTER INSERT OR UPDATE OR DELETE ON trace.load_lot
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('load_id', 'lot_id');
DROP TRIGGER IF EXISTS load_step_audit ON trace.load_step;
CREATE TRIGGER load_step_audit AFTER INSERT OR UPDATE OR DELETE ON trace.load_step
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('step_id');
DROP TRIGGER IF EXISTS delivery_audit ON trace.delivery;
CREATE TRIGGER delivery_audit AFTER INSERT OR UPDATE OR DELETE ON trace.delivery
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('delivery_id');
DROP TRIGGER IF EXISTS delivery_lot_audit ON trace.delivery_lot;
CREATE TRIGGER delivery_lot_audit AFTER INSERT OR UPDATE OR DELETE ON trace.delivery_lot
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('delivery_id', 'lot_id');

-- 6. Non-finite numbers are not measurements.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'analysis_result_value_finite') THEN
    ALTER TABLE lab.analysis_result ADD CONSTRAINT analysis_result_value_finite
      CHECK (value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conrelid = 'trace.measurement'::regclass
                    AND conname IN ('measurement_finite', 'measurement_value_finite')) THEN
    ALTER TABLE trace.measurement ADD CONSTRAINT measurement_value_finite
      CHECK (value IS NULL OR value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric));
  END IF;
END $$;

-- 7. Privileges that bypassed the timeline. Repeatable grant files must repeat these REVOKEs
--    after their broad GRANTs, because they re-run whenever they change.
REVOKE UPDATE, DELETE, TRUNCATE ON lab.analysis FROM role_lab, role_ingest, role_operator, role_quality;
REVOKE DELETE, TRUNCATE ON lab.parameter_limit FROM role_quality, role_ingest;
