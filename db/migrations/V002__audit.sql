-- V002 audit trail: who changed which row, when, from what to what
CREATE TABLE audit.change_log (
  change_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  changed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- apps run "SET app.user = 'k.amer'" after connecting; fall back to the login role
  changed_by  text        NOT NULL DEFAULT coalesce(nullif(current_setting('app.user', true), ''), session_user),
  table_name  text        NOT NULL,
  operation   text        NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
  row_pk      jsonb       NOT NULL,
  old_row     jsonb,
  new_row     jsonb
);
CREATE INDEX change_log_table_time_idx ON audit.change_log (table_name, changed_at);

-- Generic AFTER ROW trigger. Pass the primary key column names as trigger arguments.
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
  src jsonb;
  pk  jsonb := '{}'::jsonb;
  i   int;
BEGIN
  src := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
  FOR i IN 0 .. TG_NARGS - 1 LOOP
    pk := pk || jsonb_build_object(TG_ARGV[i], src -> TG_ARGV[i]);
  END LOOP;
  -- skip UPDATEs that changed nothing
  IF TG_OP = 'UPDATE' AND to_jsonb(OLD) = to_jsonb(NEW) THEN
    RETURN NULL;
  END IF;
  INSERT INTO audit.change_log (table_name, operation, row_pk, old_row, new_row)
  VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, pk,
          CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END);
  RETURN NULL;
END $$;
