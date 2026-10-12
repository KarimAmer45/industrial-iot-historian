-- Reconstruct unchanged interval readings before calculating energy totals.
-- Source timestamps are preserved literally, including each date's midnight row.
CREATE VIEW hist.v_steel_grid AS
SELECT d.ts,c.value AS usage_kwh,c.quality
FROM hist.recording r JOIN hist.signal g USING(recording_id)
CROSS JOIN LATERAL generate_series(r.starts_at,r.ends_at,interval '15 minutes') d(ts)
LEFT JOIN LATERAL (SELECT value,quality FROM hist.sample s WHERE s.signal_id=g.signal_id AND s.ts<=d.ts ORDER BY ts DESC LIMIT 1) c ON true
WHERE r.recording_id='steel2018' AND g.tag='SteelPlant.MainMeter.Usage_kWh';
CREATE VIEW hist.v_steel_daily AS SELECT (ts AT TIME ZONE 'UTC')::date AS source_date,
 sum(usage_kwh) FILTER(WHERE quality=0) AS energy_kwh,count(*) AS intervals,
 count(*) FILTER(WHERE quality=0) AS good_intervals
FROM hist.v_steel_grid GROUP BY 1;
GRANT SELECT ON hist.v_steel_grid,hist.v_steel_daily TO role_readonly;
