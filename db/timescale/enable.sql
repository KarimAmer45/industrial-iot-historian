CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT create_hypertable('hist.sample',by_range('ts',INTERVAL '1 day'),migrate_data=>true,if_not_exists=>true);
ALTER TABLE hist.sample SET(timescaledb.enable_columnstore=true,timescaledb.segmentby='signal_id',timescaledb.orderby='ts DESC');
CALL add_columnstore_policy('hist.sample',after=>INTERVAL '7 days',if_not_exists=>true);
-- Old replay timestamps must not be removed by a retention policy.
CREATE MATERIALIZED VIEW IF NOT EXISTS hist.sample_1h WITH(timescaledb.continuous) AS
SELECT signal_id,time_bucket(INTERVAL '1 hour',ts) AS bucket,avg(value) AS avg_value,
 min(value) AS min_value,max(value) AS max_value,count(*) AS n
FROM hist.sample GROUP BY signal_id,bucket WITH NO DATA;
ALTER MATERIALIZED VIEW hist.sample_1h SET(timescaledb.materialized_only=false);
SELECT add_continuous_aggregate_policy('hist.sample_1h',start_offset=>INTERVAL '30 days',end_offset=>INTERVAL '1 hour',schedule_interval=>INTERVAL '1 hour',if_not_exists=>true);
GRANT SELECT ON hist.sample_1h TO role_readonly;
