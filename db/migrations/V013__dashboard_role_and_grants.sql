-- Grants for the objects added after V011, a dashboard role limited to what the panels read,
-- and the evidence-hardening REVOKEs repeated after every grant in this project.
-- Keep this the last grant statement in the migration order: a later file that grants broadly
-- must repeat the REVOKEs at its end.

GRANT SELECT ON hist.acquisition_gap,hist.sample_quarantine TO role_readonly;

-- Grafana's login gets only this role. The views run with their owner's rights; hist.integral
-- (used by v_lab_comparison) reads hist.sample and hist.acquisition_gap with the caller's rights.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'role_dashboard') THEN
    CREATE ROLE role_dashboard NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA hist TO role_dashboard;
GRANT SELECT ON hist.sample,hist.signal,hist.acquisition_gap,hist.v_skab_scored,hist.v_ah_ratio,
 hist.v_scores,hist.v_lab_comparison,hist.v_steel_daily TO role_dashboard;

REVOKE UPDATE, DELETE, TRUNCATE ON lab.analysis FROM role_lab, role_ingest, role_operator, role_quality;
REVOKE DELETE, TRUNCATE ON lab.parameter_limit FROM role_quality, role_ingest;
