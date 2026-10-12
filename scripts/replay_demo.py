"""Run real TCP replay and collector together, then compare every received change."""
import asyncio,json,logging,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'ingest'),str(ROOT/'scripts')]
from common import connect
from recordings import register_recordings
from opcua_replay_server import prepare,replay
from opcua_collector import collect
from timescale import refresh_range

async def main():
    register_recordings()
    server,streams=await prepare('opc.tcp://127.0.0.1:4843/historian/')
    stop=asyncio.Event();ready=asyncio.Event()
    async with server:
        collector=asyncio.create_task(collect('opc.tcp://127.0.0.1:4843/historian/',['SKAB','SteelPlant'],stop,ready))
        try:
            await asyncio.wait_for(ready.wait(),30)
            producer=asyncio.gather(*(replay(s,.005,20 if i==len(streams)-1 else 1) for i,s in enumerate(streams)))
            done,_=await asyncio.wait([producer,collector],return_when=asyncio.FIRST_COMPLETED)
            if collector in done:
                producer.cancel();await collector;raise RuntimeError('Collector stopped before replay ended')
            await producer;await asyncio.sleep(2)
        finally:stop.set()
        report=await collector
    counts=[]
    with connect('verify_replay') as conn:
        conn.execute("UPDATE hist.signal SET low_limit=b.low_limit,high_limit=b.high_limit FROM hist.baseline b WHERE b.parameter='Current' AND tag LIKE 'SKAB.%.WaterLoop.Current'")
        for rows,variables in streams:
            for col,node in variables.items():
                meta=json.loads((await node.read_description()).Text)
                tag='SteelPlant.MainMeter.'+col.replace(' ','') if meta['recording_id']=='steel2018' else 'SKAB.'+meta['recording_id']+('.Labels.' if col in ('anomaly','changepoint') else '.WaterLoop.')+col.replace(' ','')
                actual=conn.execute('SELECT ts,value FROM hist.sample JOIN hist.signal USING(signal_id) WHERE tag=%s ORDER BY ts',(tag,)).fetchall()
                expected=[(r['ts'],r[col]) for i,r in enumerate(rows) if i==0 or r[col]!=rows[i-1][col]]
                assert actual==expected,f'Acquisition mismatch {tag}: expected {len(expected)}, got {len(actual)}'
                counts.append({'tag':tag,'source_rows':len(rows),'expected_changes':len(expected),'stored':len(actual),'all_changes_match':True})
        report['scores']=[dict(zip([c.name for c in cur.description],row)) for cur in [conn.execute('SELECT * FROM hist.v_scores ORDER BY recording_id')] for row in cur.fetchall()]
        report['acquisition_gaps']=[dict(zip(['reason','closed','signals'],r)) for r in conn.execute('SELECT reason,to_ts IS NOT NULL,count(*) FROM hist.acquisition_gap g JOIN hist.signal s USING(signal_id) WHERE s.recording_id IS NOT NULL GROUP BY 1,2 ORDER BY 1,2')]
        ranges=conn.execute('SELECT recording_id,starts_at,ends_at FROM hist.recording ORDER BY starts_at').fetchall()
    # Replayed history is older than the aggregate policy's 30-day window: refresh exactly these ranges.
    report['aggregate_refreshed']=[rid for rid,a,b in ranges if refresh_range(a,b)]
    report['signals']=counts;report['transport']='OPC UA subscriptions over localhost TCP; no sensor backfill shortcut'
    (ROOT/'docs/results/replay_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
if __name__=='__main__':logging.basicConfig(level=logging.WARNING);asyncio.run(main())
