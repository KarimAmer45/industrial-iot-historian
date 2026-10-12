-- Expand each change interval once instead of 35,040 backwards probes over chunks.
CREATE OR REPLACE VIEW hist.v_steel_grid AS
WITH changes AS (
 SELECT s.ts,s.value,s.quality,r.ends_at,
 lead(s.ts,1,r.ends_at+interval '15 minutes') OVER(ORDER BY s.ts) AS next_ts
 FROM hist.sample s JOIN hist.signal g USING(signal_id)
 JOIN hist.recording r USING(recording_id)
 WHERE r.recording_id='steel2018' AND g.tag='SteelPlant.MainMeter.Usage_kWh'
)
SELECT d.ts,c.value AS usage_kwh,c.quality FROM changes c
CROSS JOIN LATERAL generate_series(c.ts,least(c.next_ts-interval '15 minutes',c.ends_at),interval '15 minutes') d(ts);
