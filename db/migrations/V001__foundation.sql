-- V001 foundation: extensions, schemas, group roles
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- lets "=" on plain columns sit inside GiST exclusion constraints

CREATE SCHEMA IF NOT EXISTS ref;     -- master data: customers, parts, lines, tanks, stations
CREATE SCHEMA IF NOT EXISTS lab;     -- bath chemistry: parameters, limits, analyses, dosing
CREATE SCHEMA IF NOT EXISTS trace;   -- orders, lots, loads, steps, measurements, deliveries, complaints
CREATE SCHEMA IF NOT EXISTS bosch;   -- real public traceability data (Kaggle Bosch) for scale tests
CREATE SCHEMA IF NOT EXISTS realdata;-- other real public datasets (NIST, SECOM)
CREATE SCHEMA IF NOT EXISTS hist;    -- time series historian (project 3)
CREATE SCHEMA IF NOT EXISTS spec;    -- customer norms and release checks (project 4)
CREATE SCHEMA IF NOT EXISTS audit;   -- change log written by triggers

-- Group roles (NOLOGIN). Real users are created per person and granted one of these.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['role_readonly','role_operator','role_lab','role_quality','role_ingest'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;
