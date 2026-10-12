CREATE VIEW hist.v_daily_charge AS
WITH signals AS (
 SELECT signal_id,t.code AS tank_code,min(s.ts) AS starts,max(s.ts) AS ends
 FROM hist.signal g JOIN ref.station st USING(station_id) JOIN ref.tank t USING(tank_id)
 JOIN hist.sample s USING(signal_id) WHERE g.origin='simulated' GROUP BY signal_id,t.code
), days AS (
 SELECT signals.*,d::date AS day,tstzrange(d::date::timestamp AT TIME ZONE 'Europe/Berlin',
  (d::date+1)::timestamp AT TIME ZONE 'Europe/Berlin','[)') AS period
 FROM signals CROSS JOIN LATERAL generate_series((starts AT TIME ZONE 'Europe/Berlin')::date,
  (ends AT TIME ZONE 'Europe/Berlin')::date-1,interval '1 day') d
), energy AS (
 SELECT days.*,i.* FROM days CROSS JOIN LATERAL hist.integral(signal_id,period,interval '11 seconds','hold') i
) SELECT tank_code,day,sum(amp_hours) AS historian_ah,
 sum(covered_seconds)/nullif(sum(requested_seconds),0) AS coverage
FROM energy GROUP BY tank_code,day;
CREATE VIEW hist.v_lab_comparison AS
SELECT d.*,r.amp_hours AS lab_reference_ah,d.historian_ah-r.amp_hours AS difference_ah,
 (d.historian_ah/r.amp_hours-1)*100 AS difference_pct,r.source
FROM hist.v_daily_charge d LEFT JOIN hist.lab_charge_reference r USING(tank_code,day);
GRANT SELECT ON hist.v_daily_charge,hist.v_lab_comparison TO role_readonly;
