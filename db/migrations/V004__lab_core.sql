-- V004 bath chemistry core (project 1 needs it for traceability, project 2 extends it)
CREATE TABLE lab.parameter (
  parameter_code text PRIMARY KEY,   -- 'ZN','NI','NAOH','NA2CO3','COD','TEMP'
  name           text NOT NULL,
  unit           text NOT NULL
);

-- Operating window per process and parameter, versioned. Two versions may never overlap in time.
CREATE TABLE lab.parameter_limit (
  limit_id       int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  process_code   text NOT NULL REFERENCES ref.process,
  parameter_code text NOT NULL REFERENCES lab.parameter,
  lsl            numeric(12,4),
  target         numeric(12,4),
  usl            numeric(12,4),
  valid_during   daterange NOT NULL,
  source         text NOT NULL,      -- key into config/sources.yaml
  CHECK (lsl IS NOT NULL OR usl IS NOT NULL),
  CHECK (lsl IS NULL OR usl IS NULL OR lsl < usl),
  CHECK (target IS NULL OR ((lsl IS NULL OR target >= lsl) AND (usl IS NULL OR target <= usl))),
  CONSTRAINT parameter_limit_no_overlap
    EXCLUDE USING gist (process_code WITH =, parameter_code WITH =, valid_during WITH &&)
);

-- One lab analysis of one tank. valid_during = from this sample until the next sample of the same tank.
CREATE TABLE lab.analysis (
  analysis_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tank_id      int    NOT NULL REFERENCES ref.tank,
  sampled_at   timestamptz NOT NULL,
  analyst      text   NOT NULL DEFAULT coalesce(nullif(current_setting('app.user', true), ''), session_user),
  valid_during tstzrange NOT NULL,
  source       text   NOT NULL,     -- 'sim:v1', 'excel:<file>#<row>', 'manual'
  UNIQUE (tank_id, sampled_at),
  CONSTRAINT analysis_starts_at_sample CHECK (NOT isempty(valid_during) AND lower(valid_during) = sampled_at),
  CONSTRAINT analysis_no_overlap EXCLUDE USING gist (tank_id WITH =, valid_during WITH &&)
);

-- Keeps validity ranges consistent on insert, including back-filled (older) samples.
-- The exclusion constraint above still guarantees correctness if two sessions race.
CREATE OR REPLACE FUNCTION lab.set_analysis_validity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE next_sample timestamptz;
BEGIN
  -- Same tank and time already stored: change nothing, let the UNIQUE constraint
  -- (or INSERT ... ON CONFLICT) decide. Without this guard an ON CONFLICT insert would
  -- shrink the existing row's range to empty before the conflict is detected.
  IF EXISTS (SELECT 1 FROM lab.analysis WHERE tank_id = NEW.tank_id AND sampled_at = NEW.sampled_at) THEN
    NEW.valid_during := tstzrange(NEW.sampled_at, NULL, '[)');
    RETURN NEW;
  END IF;

  SELECT min(sampled_at) INTO next_sample
    FROM lab.analysis
   WHERE tank_id = NEW.tank_id AND sampled_at > NEW.sampled_at;

  NEW.valid_during := tstzrange(NEW.sampled_at, next_sample, '[)');   -- NULL upper = open ended

  UPDATE lab.analysis
     SET valid_during = tstzrange(lower(valid_during), NEW.sampled_at, '[)')
   WHERE tank_id = NEW.tank_id
     AND valid_during @> NEW.sampled_at;
  RETURN NEW;
END $$;

CREATE TRIGGER analysis_validity BEFORE INSERT ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION lab.set_analysis_validity();

CREATE TABLE lab.analysis_result (
  analysis_id    bigint NOT NULL REFERENCES lab.analysis ON DELETE CASCADE,
  parameter_code text   NOT NULL REFERENCES lab.parameter,
  value          numeric(12,4) NOT NULL,
  method         text   NOT NULL CHECK (method IN ('titration','AAS','XRF','ICP','COD','thermometer','other')),
  PRIMARY KEY (analysis_id, parameter_code)
);

CREATE TRIGGER analysis_result_audit AFTER INSERT OR UPDATE OR DELETE ON lab.analysis_result
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('analysis_id', 'parameter_code');

-- Every result with the limit that applied on the sampling day and its status
CREATE VIEW lab.v_result_status AS
SELECT a.analysis_id, a.tank_id, t.code AS tank_code, t.process_code, a.sampled_at, a.valid_during,
       r.parameter_code, r.value, l.lsl, l.target, l.usl,
       CASE
         WHEN l.limit_id IS NULL THEN 'NO_LIMIT'
         WHEN l.lsl IS NOT NULL AND r.value < l.lsl THEN 'LOW'
         WHEN l.usl IS NOT NULL AND r.value > l.usl THEN 'HIGH'
         ELSE 'OK'
       END AS status
  FROM lab.analysis a
  JOIN ref.tank t            ON t.tank_id = a.tank_id
  JOIN lab.analysis_result r ON r.analysis_id = a.analysis_id
  LEFT JOIN lab.parameter_limit l
         ON l.process_code = t.process_code
        AND l.parameter_code = r.parameter_code
        AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date;

-- Periods in which a tank ran on an analysis with at least one out of spec result
CREATE VIEW lab.v_out_of_spec_period AS
SELECT tank_id, tank_code, analysis_id, valid_during,
       string_agg(parameter_code || '=' || value || ' ' || status, ', ' ORDER BY parameter_code) AS findings
  FROM lab.v_result_status
 WHERE status IN ('LOW','HIGH')
 GROUP BY tank_id, tank_code, analysis_id, valid_during;
