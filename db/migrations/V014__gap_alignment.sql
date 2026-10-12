-- Same gap convention in every view: from from_ts on a held value is unknown; only a sample stored
-- exactly at from_ts stays valid at that instant (V012 let scoring use the older value at from_ts itself).
CREATE OR REPLACE VIEW hist.v_skab_scored AS
SELECT p.recording_id,p.ts,p.anomaly,
 CASE WHEN h.held THEN c.value END AS current_a,CASE WHEN h.held THEN c.quality END AS quality,
 CASE WHEN h.held AND c.quality=0 THEN coalesce(c.value<g.low_limit,false) OR coalesce(c.value>g.high_limit,false) END AS predicted
FROM hist.recording_point p JOIN hist.signal g ON g.recording_id=p.recording_id AND g.tag LIKE 'SKAB.%.WaterLoop.Current'
LEFT JOIN LATERAL (SELECT ts,value,quality FROM hist.sample s WHERE s.signal_id=g.signal_id AND s.ts<=p.ts
 ORDER BY ts DESC LIMIT 1) c ON true
LEFT JOIN LATERAL (SELECT c.ts=p.ts OR ((g.max_hold IS NULL OR p.ts<c.ts+g.max_hold)
 AND NOT EXISTS(SELECT 1 FROM hist.acquisition_gap a WHERE a.signal_id=g.signal_id AND a.from_ts<=p.ts
  AND (a.to_ts IS NULL OR a.to_ts>c.ts))) AS held) h ON true
WHERE p.anomaly IS NOT NULL;

-- Excursions are consecutive observed points; an acquisition gap between two points breaks the run
-- just like a recorded gap over five seconds.
CREATE OR REPLACE VIEW hist.v_excursion AS
WITH f AS (
 SELECT s.*,g.tag,g.recording_id,
  quality=0 AND (coalesce(value<low_limit,false) OR coalesce(value>high_limit,false)) AS outside,
  lag(ts) OVER(PARTITION BY signal_id ORDER BY ts) AS prev_ts
 FROM hist.sample s JOIN hist.signal g USING(signal_id)
 WHERE low_limit IS NOT NULL OR high_limit IS NOT NULL
), breaks AS (
 SELECT *,CASE WHEN outside IS DISTINCT FROM lag(outside) OVER(PARTITION BY signal_id ORDER BY ts)
  OR ts-prev_ts>interval '5 seconds'
  OR EXISTS(SELECT 1 FROM hist.acquisition_gap a WHERE a.signal_id=f.signal_id AND a.from_ts<f.ts
   AND (a.to_ts IS NULL OR a.to_ts>f.prev_ts)) THEN 1 ELSE 0 END AS boundary FROM f
), grouped AS (
 SELECT *,sum(boundary) OVER(PARTITION BY signal_id ORDER BY ts) AS grp FROM breaks
)
SELECT signal_id,tag,recording_id,min(ts) AS started,max(ts) AS ended,count(*) AS samples,
 min(value) AS min_value,max(value) AS max_value FROM grouped WHERE outside GROUP BY signal_id,tag,recording_id,grp;
