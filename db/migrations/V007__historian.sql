CREATE TABLE hist.recording (
 recording_id text PRIMARY KEY, source text NOT NULL, origin text NOT NULL CHECK(origin IN ('real','simulated')),
 starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, rows int NOT NULL,
 timestamp_basis text NOT NULL, CHECK(ends_at>=starts_at)
);
CREATE TABLE hist.signal (
 signal_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,tag text NOT NULL UNIQUE,
 description text,unit text,origin text NOT NULL CHECK(origin IN ('real','simulated')),source text NOT NULL,
 station_id int REFERENCES ref.station,recording_id text REFERENCES hist.recording,
 low_limit double precision,high_limit double precision,
 CHECK(low_limit IS NULL OR high_limit IS NULL OR low_limit<high_limit)
);
CREATE TABLE hist.sample (
 signal_id int NOT NULL REFERENCES hist.signal,ts timestamptz NOT NULL,
 value double precision NOT NULL CHECK(value>'-Infinity'::float8 AND value<'Infinity'::float8),
 quality smallint NOT NULL DEFAULT 0 CHECK(quality IN (0,1)),PRIMARY KEY(signal_id,ts)
);
CREATE TABLE hist.recording_point (
 recording_id text REFERENCES hist.recording,ts timestamptz,anomaly boolean,
 PRIMARY KEY(recording_id,ts)
);
CREATE TABLE hist.baseline (
 parameter text PRIMARY KEY,n int,mean float8,sd float8,low_limit float8,high_limit float8,source text NOT NULL
);
CREATE TABLE hist.step_energy (
 step_id bigint PRIMARY KEY REFERENCES trace.load_step,signal_id int REFERENCES hist.signal,
 planned_ah float8 NOT NULL,measured_ah float8,coverage float8 NOT NULL,
 method text NOT NULL,fault_expected boolean NOT NULL
);
CREATE TABLE hist.lab_charge_reference (
 tank_code text,day date,amp_hours numeric NOT NULL,source text NOT NULL,
 PRIMARY KEY(tank_code,day)
);

-- Clip intervals to exact window boundaries; never infer beyond the next observation.
-- A bad/uncertain sample or excessive gap excludes that interval from coverage.
CREATE FUNCTION hist.integral(p_signal int,p_window tstzrange,p_max_gap interval DEFAULT interval '30 seconds',p_method text DEFAULT 'linear')
RETURNS TABLE(amp_hours float8,covered_seconds float8,requested_seconds float8)
LANGUAGE plpgsql STABLE AS $$
BEGIN
 IF p_method NOT IN ('linear','hold') OR p_max_gap<=interval '0' OR isempty(p_window)
 OR lower_inf(p_window) OR upper_inf(p_window) THEN RAISE EXCEPTION 'Invalid integration request'; END IF;
 RETURN QUERY
 WITH points AS (
  SELECT ts,value,quality,lead(ts) OVER(ORDER BY ts) AS next_ts,
   lead(value) OVER(ORDER BY ts) AS next_value,lead(quality) OVER(ORDER BY ts) AS next_quality
  FROM hist.sample WHERE signal_id=p_signal
   AND ts>=lower(p_window)-p_max_gap AND ts<=upper(p_window)+p_max_gap
 ), segments AS (
  SELECT *,greatest(ts,lower(p_window)) AS a,least(next_ts,upper(p_window)) AS b
  FROM points WHERE quality=0 AND next_ts>ts AND next_ts-ts<=p_max_gap
   AND (p_method='hold' OR next_quality=0)
 ), valid AS (
  SELECT *,extract(epoch FROM b-a)::float8 AS seconds,
   value+(next_value-value)*extract(epoch FROM a-ts)/extract(epoch FROM next_ts-ts) AS va,
   value+(next_value-value)*extract(epoch FROM b-ts)/extract(epoch FROM next_ts-ts) AS vb
  FROM segments WHERE b>a
 ) SELECT sum(CASE WHEN p_method='hold' THEN value ELSE (va+vb)/2 END*seconds/3600)::float8,
   coalesce(sum(seconds),0)::float8,extract(epoch FROM upper(p_window)-lower(p_window))::float8 FROM valid;
END $$;
CREATE FUNCTION hist.integrate_hours(p_signal int,p_window tstzrange,p_max_gap interval DEFAULT interval '30 seconds',p_method text DEFAULT 'linear')
RETURNS float8 LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN abs(covered_seconds-requested_seconds)<0.00001 THEN amp_hours END
 FROM hist.integral(p_signal,p_window,p_max_gap,p_method) $$;

-- Excursions are consecutive observed points, with large acquisition gaps breaking runs.
CREATE VIEW hist.v_excursion AS
WITH f AS (
 SELECT s.*,g.tag,g.recording_id,
  quality=0 AND (coalesce(value<low_limit,false) OR coalesce(value>high_limit,false)) AS outside,
  lag(ts) OVER(PARTITION BY signal_id ORDER BY ts) AS prev_ts
 FROM hist.sample s JOIN hist.signal g USING(signal_id)
 WHERE low_limit IS NOT NULL OR high_limit IS NOT NULL
), breaks AS (
 SELECT *,CASE WHEN outside IS DISTINCT FROM lag(outside) OVER(PARTITION BY signal_id ORDER BY ts)
  OR ts-prev_ts>interval '5 seconds' THEN 1 ELSE 0 END AS boundary FROM f
), grouped AS (
 SELECT *,sum(boundary) OVER(PARTITION BY signal_id ORDER BY ts) AS grp FROM breaks
)
SELECT signal_id,tag,recording_id,min(ts) AS started,max(ts) AS ended,count(*) AS samples,
 min(value) AS min_value,max(value) AS max_value FROM grouped WHERE outside GROUP BY signal_id,tag,recording_id,grp;

CREATE VIEW hist.v_skab_scored AS
SELECT p.recording_id,p.ts,p.anomaly,c.value AS current_a,c.quality,
 CASE WHEN c.quality=0 THEN coalesce(c.value<g.low_limit,false) OR coalesce(c.value>g.high_limit,false) END AS predicted
FROM hist.recording_point p JOIN hist.signal g ON g.recording_id=p.recording_id AND g.tag LIKE 'SKAB.%.WaterLoop.Current'
LEFT JOIN LATERAL (SELECT value,quality FROM hist.sample s WHERE s.signal_id=g.signal_id AND s.ts<=p.ts
 ORDER BY ts DESC LIMIT 1) c ON true WHERE p.anomaly IS NOT NULL;
CREATE VIEW hist.v_scores AS
WITH counts AS (
 SELECT recording_id,count(*) AS total,count(predicted) AS scored,
 count(*) FILTER(WHERE predicted AND anomaly) AS tp,
 count(*) FILTER(WHERE predicted AND NOT anomaly) AS fp,
 count(*) FILTER(WHERE NOT predicted AND anomaly) AS fn,
 count(*) FILTER(WHERE NOT predicted AND NOT anomaly) AS tn
 FROM hist.v_skab_scored GROUP BY recording_id
) SELECT *,tp::float8/nullif(tp+fp,0) AS precision,tp::float8/nullif(tp+fn,0) AS recall,
 2*tp::float8/nullif(2*tp+fp+fn,0) AS f1 FROM counts;
CREATE VIEW hist.v_ah_ratio AS
SELECT e.*,s.code AS station,l.carrier_no,lower(st.in_station) AS started,upper(st.in_station) AS ended,
 measured_ah/nullif(planned_ah,0) AS ratio
FROM hist.step_energy e JOIN trace.load_step st USING(step_id)
JOIN ref.station s USING(station_id) JOIN trace.load l USING(load_id);
CREATE VIEW hist.v_signal_counts AS SELECT g.tag,g.origin,g.source,count(s.ts) AS samples,min(s.ts) AS first_ts,max(s.ts) AS last_ts
FROM hist.signal g LEFT JOIN hist.sample s USING(signal_id) GROUP BY g.signal_id;
GRANT USAGE ON SCHEMA hist,ref,trace TO role_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA hist,ref,trace TO role_readonly;
