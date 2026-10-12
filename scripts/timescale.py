"""Enable Timescale, refresh aggregates for loaded ranges and measure columnstore bytes per dataset."""
import argparse,json,pathlib,re,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

DATASET="""CASE WHEN g.recording_id='steel2018' THEN 'steel meter (real)' WHEN g.recording_id LIKE 'valve%' THEN 'SKAB rig (real)'
 WHEN g.station_id IS NOT NULL THEN 'rectifiers (simulated)' WHEN g.tag LIKE 'Milo.%' THEN 'Milo (simulated, live)' ELSE 'other' END"""

def enable():
    with connect('enable_timescale') as conn:conn.execute((ROOT/'db/timescale/enable.sql').read_text())

def refresh_range(start,end):
    """Materialize the hourly aggregate for exactly the whole hours touched by [start, end].
    The policy only refreshes the last 30 days, so replayed or backfilled history needs this call."""
    if start is None or end is None:return False
    conn=connect('refresh_aggregate');conn.commit();conn.autocommit=True
    try:
        if not conn.execute("SELECT to_regclass('hist.sample_1h')").fetchone()[0]:return False
        a,b=conn.execute("SELECT time_bucket('1 hour',%s::timestamptz),time_bucket('1 hour',%s::timestamptz)+interval '1 hour'",(start,end)).fetchone()
        conn.execute("CALL refresh_continuous_aggregate('hist.sample_1h',%s::timestamptz,%s::timestamptz)",(a,b));return True
    finally:conn.close()

def fresh_rowstore_bytes(conn,chunk,indexes):
    """Size of the chunk's rows in a freshly written heap with freshly built copies of the hypertable's indexes.
    Converting columnstore chunks back to rowstore leaves bloated indexes, so that size depends on history."""
    conn.execute('CREATE TEMP TABLE fresh_rowstore (LIKE hist.sample INCLUDING DEFAULTS INCLUDING CONSTRAINTS)')
    try:
        conn.execute(f'INSERT INTO fresh_rowstore SELECT * FROM {chunk} ORDER BY ts,signal_id')
        for definition in indexes:conn.execute(re.sub(r'^CREATE (UNIQUE )?INDEX \S+ ON hist\.sample ',r'CREATE \1INDEX ON fresh_rowstore ',definition))
        return conn.execute("SELECT pg_total_relation_size('fresh_rowstore')").fetchone()[0]
    finally:conn.execute('DROP TABLE fresh_rowstore')

def measure():
    conn=connect('measure_timescale');conn.commit();conn.autocommit=True
    try:
        count_before=conn.execute('SELECT count(*) FROM hist.sample').fetchone()[0]
        chunks=[c for (c,) in conn.execute("SELECT show_chunks('hist.sample')::text").fetchall()]
        # Physically convert every chunk (recompressing partial ones) so the columnstore figure is complete.
        for chunk in chunks:conn.execute('CALL convert_to_columnstore(%s::regclass,if_not_columnstore=>true,recompress=>true)',(chunk,))
        # Compressed relations only: the empty rowstore relations each converted chunk keeps vary with its history.
        footprint=conn.execute("SELECT hypertable_size('hist.sample')").fetchone()[0]
        compressed=dict(conn.execute("SELECT chunk_schema||'.'||chunk_name,coalesce(after_compression_total_bytes,0) FROM chunk_columnstore_stats('hist.sample')").fetchall())
        indexes=[r[0] for r in conn.execute("SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indrelid='hist.sample'::regclass ORDER BY indexrelid")]
        datasets={}
        for chunk in chunks:
            names=conn.execute(f'SELECT DISTINCT {DATASET} FROM {chunk} s JOIN hist.signal g USING(signal_id)').fetchall()
            name=names[0][0] if len(names)==1 else 'mixed' if names else 'empty'
            d=datasets.setdefault(name,{'dataset':name,'chunks':0,'rows':0,'rowstore_bytes':0,'columnstore_bytes':0})
            d['chunks']+=1;d['rows']+=conn.execute(f'SELECT count(*) FROM {chunk}').fetchone()[0]
            d['rowstore_bytes']+=fresh_rowstore_bytes(conn,chunk,indexes);d['columnstore_bytes']+=compressed[chunk]
        for d in datasets.values():d['compression_ratio']=d['rowstore_bytes']/d['columnstore_bytes']
        rowstore=sum(d['rowstore_bytes'] for d in datasets.values());columnstore=sum(d['columnstore_bytes'] for d in datasets.values())
        count_after=conn.execute('SELECT count(*) FROM hist.sample').fetchone()[0]
        # No full refresh here: the replay and backfill scripts refresh their own ranges, which this check verifies.
        aggregate=conn.execute('SELECT sum(n),count(*) FROM hist.sample_1h').fetchone()
        delta=conn.execute("SELECT count(*) FROM (SELECT signal_id,time_bucket('1 hour',ts) AS bucket,avg(value) AS a,count(*) AS n FROM hist.sample GROUP BY 1,2) raw FULL JOIN hist.sample_1h h USING(signal_id,bucket) WHERE raw.n IS DISTINCT FROM h.n OR abs(raw.a-h.avg_value)>greatest(1e-9,1e-12*abs(raw.a))").fetchone()[0]
        assert count_before==count_after==aggregate[0] and delta==0, (count_before,count_after,aggregate,delta)
        report={'postgres_version':conn.execute('SHOW server_version').fetchone()[0],
          'timescale_version':conn.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'").fetchone()[0],
          'sample_count':count_after,'chunks':len(chunks),'rowstore_bytes':rowstore,'columnstore_bytes':columnstore,
          'compression_ratio':rowstore/columnstore,
          'rowstore_method':'each chunk copied into a fresh heap with freshly built copies of the hypertable indexes',
          'columnstore_method':'compressed chunk relations (chunk_columnstore_stats after_compression_total_bytes)',
          'hypertable_size_after_conversion':footprint,
          'hypertable_size_note':'also counts the empty rowstore relations of every converted chunk; their size depends on conversion history',
          'datasets':sorted(datasets.values(),key=lambda d:-d['rows']),
          'hourly_buckets':aggregate[1],'aggregate_count':int(aggregate[0]),'aggregate_mismatches':delta,
          'aggregate_refresh':'range refresh after replay and rectifier backfill; hourly policy for the last 30 days',
          'average_tolerance':'max(1e-9 absolute, 1e-12 relative); sample counts exact'}
        (ROOT/'docs/results/timescale.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
    finally:conn.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--refresh',nargs=2,metavar=('FROM','TO'),help='only refresh the hourly aggregate for this source-time range')
    args=ap.parse_args()
    if args.refresh:print('Refreshed' if refresh_range(*args.refresh) else 'No aggregate to refresh')
    else:enable();measure()
