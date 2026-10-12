-- Acquisition gaps, maximum hold age and quarantined duplicates.
-- Change-only storage cannot tell "unchanged" from "not acquired". The collector therefore records every
-- interval it cannot vouch for, and reconstruction views never carry a value across such a gap or
-- beyond the signal's maximum hold age.

-- change_only: a value stays valid until the next stored change, an acquisition gap or max_hold.
-- sampled: a value is valid for at most max_hold; a missing sample is missing data.
ALTER TABLE hist.signal
 ADD COLUMN acquisition_mode text NOT NULL DEFAULT 'change_only' CHECK(acquisition_mode IN ('change_only','sampled')),
 ADD COLUMN max_hold interval CHECK(max_hold>interval '0'),
 ADD CONSTRAINT signal_sampled_needs_max_hold CHECK(acquisition_mode='change_only' OR max_hold IS NOT NULL);

-- Values of the signal are unknown strictly after from_ts and before to_ts (NULL: still open).
-- A stored sample is always valid at its own timestamp; a held value never crosses from_ts.
CREATE TABLE hist.acquisition_gap (
 gap_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 signal_id int NOT NULL REFERENCES hist.signal,
 from_ts timestamptz NOT NULL,to_ts timestamptz,reason text NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now(),
 CHECK(to_ts IS NULL OR to_ts>from_ts)
);
CREATE INDEX acquisition_gap_signal_idx ON hist.acquisition_gap(signal_id,from_ts);

-- A different value for an already recorded signal/timestamp: the first stored value stands.
CREATE TABLE hist.sample_quarantine (
 quarantine_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 signal_id int NOT NULL REFERENCES hist.signal,ts timestamptz NOT NULL,
 value double precision NOT NULL CHECK(value>'-Infinity'::float8 AND value<'Infinity'::float8),
 quality smallint NOT NULL CHECK(quality IN (0,1)),
 kept_value double precision,kept_quality smallint,reason text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now()
);

-- Integration drops every interval that touches an acquisition gap (coverage below 1 -> NULL Ah).
CREATE OR REPLACE FUNCTION hist.integral(p_signal int,p_window tstzrange,p_max_gap interval DEFAULT interval '30 seconds',p_method text DEFAULT 'linear')
RETURNS TABLE(amp_hours float8,covered_seconds float8,requested_seconds float8)
LANGUAGE plpgsql STABLE AS $$
BEGIN
 IF p_method NOT IN ('linear','hold') OR p_max_gap<=interval '0' OR isempty(p_window)
 OR lower_inf(p_window) OR upper_inf(p_window) THEN RAISE EXCEPTION 'Invalid integration request'; END IF;
 RETURN QUERY
 WITH gaps AS MATERIALIZED (
  SELECT from_ts,coalesce(to_ts,'infinity') AS to_ts FROM hist.acquisition_gap
  WHERE signal_id=p_signal AND from_ts<upper(p_window)+p_max_gap AND coalesce(to_ts,'infinity')>lower(p_window)-p_max_gap
 ), points AS (
  SELECT ts,value,quality,lead(ts) OVER(ORDER BY ts) AS next_ts,
   lead(value) OVER(ORDER BY ts) AS next_value,lead(quality) OVER(ORDER BY ts) AS next_quality
  FROM hist.sample WHERE signal_id=p_signal
   AND ts>=lower(p_window)-p_max_gap AND ts<=upper(p_window)+p_max_gap
 ), segments AS (
  SELECT *,greatest(ts,lower(p_window)) AS a,least(next_ts,upper(p_window)) AS b
  FROM points WHERE quality=0 AND next_ts>ts AND next_ts-ts<=p_max_gap
   AND (p_method='hold' OR next_quality=0)
   AND NOT EXISTS(SELECT 1 FROM gaps g WHERE g.from_ts<points.next_ts AND g.to_ts>points.ts)
 ), valid AS (
  SELECT *,extract(epoch FROM b-a)::float8 AS seconds,
   value+(next_value-value)*extract(epoch FROM a-ts)/extract(epoch FROM next_ts-ts) AS va,
   value+(next_value-value)*extract(epoch FROM b-ts)/extract(epoch FROM next_ts-ts) AS vb
  FROM segments WHERE b>a
 ) SELECT sum(CASE WHEN p_method='hold' THEN value ELSE (va+vb)/2 END*seconds/3600)::float8,
   coalesce(sum(seconds),0)::float8,extract(epoch FROM upper(p_window)-lower(p_window))::float8 FROM valid;
END $$;

-- Scoring: the latest current value counts only if it is still held at the labelled point.
CREATE OR REPLACE VIEW hist.v_skab_scored AS
SELECT p.recording_id,p.ts,p.anomaly,
 CASE WHEN h.held THEN c.value END AS current_a,CASE WHEN h.held THEN c.quality END AS quality,
 CASE WHEN h.held AND c.quality=0 THEN coalesce(c.value<g.low_limit,false) OR coalesce(c.value>g.high_limit,false) END AS predicted
FROM hist.recording_point p JOIN hist.signal g ON g.recording_id=p.recording_id AND g.tag LIKE 'SKAB.%.WaterLoop.Current'
LEFT JOIN LATERAL (SELECT ts,value,quality FROM hist.sample s WHERE s.signal_id=g.signal_id AND s.ts<=p.ts
 ORDER BY ts DESC LIMIT 1) c ON true
LEFT JOIN LATERAL (SELECT c.ts=p.ts OR ((g.max_hold IS NULL OR p.ts<c.ts+g.max_hold)
 AND NOT EXISTS(SELECT 1 FROM hist.acquisition_gap a WHERE a.signal_id=g.signal_id AND a.from_ts<p.ts
  AND (a.to_ts IS NULL OR a.to_ts>c.ts))) AS held) h ON true
WHERE p.anomaly IS NOT NULL;

-- Chance baselines next to the detector: prevalence, false-positive rate and the F1 of a
-- classifier that flags every point (precision = prevalence, recall = 1).
CREATE OR REPLACE VIEW hist.v_scores AS
WITH counts AS (
 SELECT recording_id,count(*) AS total,count(predicted) AS scored,
 count(*) FILTER(WHERE predicted AND anomaly) AS tp,
 count(*) FILTER(WHERE predicted AND NOT anomaly) AS fp,
 count(*) FILTER(WHERE NOT predicted AND anomaly) AS fn,
 count(*) FILTER(WHERE NOT predicted AND NOT anomaly) AS tn
 FROM hist.v_skab_scored GROUP BY recording_id
) SELECT *,tp::float8/nullif(tp+fp,0) AS precision,tp::float8/nullif(tp+fn,0) AS recall,
 2*tp::float8/nullif(2*tp+fp+fn,0) AS f1,
 (tp+fn)::float8/nullif(tp+fp+fn+tn,0) AS prevalence,
 fp::float8/nullif(fp+tn,0) AS fpr,
 tp::float8/nullif(tp+fn,0)-fp::float8/nullif(fp+tn,0) AS tpr_minus_fpr,
 2*(tp+fn)::float8/nullif(2*(tp+fn)+fp+tn,0) AS always_anomaly_f1 FROM counts;

-- Steel meter: every quarter-hour of the recording, held from the latest change but never across a
-- gap or beyond max_hold. Uncovered quarter-hours stay in the grid with NULL value and quality.
CREATE OR REPLACE VIEW hist.v_steel_grid AS
WITH meter AS (
 SELECT g.signal_id,g.max_hold,r.starts_at,r.ends_at FROM hist.signal g JOIN hist.recording r USING(recording_id)
 WHERE r.recording_id='steel2018' AND g.tag='SteelPlant.MainMeter.Usage_kWh'
), changes AS (
 SELECT s.ts,s.value,s.quality,m.signal_id,m.ends_at,
  least(lead(s.ts,1,'infinity') OVER(ORDER BY s.ts),
   CASE WHEN m.max_hold IS NULL THEN 'infinity' ELSE s.ts+m.max_hold END) AS held_until
 FROM hist.sample s JOIN meter m USING(signal_id)
), bounded AS (
 SELECT c.*,least(c.held_until,(SELECT min(greatest(a.from_ts,c.ts)) FROM hist.acquisition_gap a
  WHERE a.signal_id=c.signal_id AND a.from_ts<c.held_until AND (a.to_ts IS NULL OR a.to_ts>c.ts))) AS hold_end
 FROM changes c
), held AS (
 SELECT d.ts,b.value,b.quality FROM bounded b
 CROSS JOIN LATERAL generate_series(b.ts,greatest(b.ts,least(b.hold_end-interval '1 microsecond',b.ends_at)),interval '15 minutes') d(ts)
 WHERE b.ts<=b.ends_at
)
SELECT q.ts,h.value AS usage_kwh,h.quality FROM meter m
CROSS JOIN LATERAL generate_series(m.starts_at,m.ends_at,interval '15 minutes') q(ts)
LEFT JOIN held h USING(ts);

-- A day's total exists only when all of its quarter-hours are covered by trusted values.
CREATE OR REPLACE VIEW hist.v_steel_daily AS SELECT (ts AT TIME ZONE 'UTC')::date AS source_date,
 CASE WHEN count(*) FILTER(WHERE quality=0)=count(*) THEN sum(usage_kwh) END AS energy_kwh,count(*) AS intervals,
 count(*) FILTER(WHERE quality=0) AS good_intervals,
 count(*) FILTER(WHERE quality=0)::float8/count(*) AS coverage
FROM hist.v_steel_grid GROUP BY 1;

-- Differences to the lab reference are only meaningful for completely covered days.
CREATE OR REPLACE VIEW hist.v_lab_comparison AS
SELECT d.*,r.amp_hours AS lab_reference_ah,
 CASE WHEN abs(d.coverage-1)<1e-9 THEN d.historian_ah-r.amp_hours END AS difference_ah,
 CASE WHEN abs(d.coverage-1)<1e-9 THEN (d.historian_ah/r.amp_hours-1)*100 END AS difference_pct,r.source
FROM hist.v_daily_charge d LEFT JOIN hist.lab_charge_reference r USING(tank_code,day);
