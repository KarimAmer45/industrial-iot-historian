-- Faults are found from measured energy, independent of the planted-fault flag.
SELECT * FROM hist.v_ah_ratio WHERE ratio NOT BETWEEN .97 AND 1.03 ORDER BY started;
-- Independent baseline, pointwise metrics, observed-point excursion boundaries.
SELECT * FROM hist.baseline;
SELECT * FROM hist.v_scores;
SELECT * FROM hist.v_excursion ORDER BY tag,started;
-- Project 2 reference comparison; complete windows and consistent units.
SELECT * FROM hist.v_lab_comparison ORDER BY day;
-- Changes versus source rows are explained by the replay verification report.
SELECT * FROM hist.v_signal_counts ORDER BY tag;
SELECT * FROM hist.v_steel_daily ORDER BY source_date;
SELECT * FROM hypertable_columnstore_stats('hist.sample');
SELECT sum(n) AS aggregate_samples,count(*) AS buckets FROM hist.sample_1h;
