"""Deterministic physical rectifier model, explicit switch boundaries, complete coverage."""
import bisect,json,random,sys
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from common import ROOT,connect,sources
sys.path.insert(0,str(ROOT/'scripts'))
from timescale import refresh_range
TZ=ZoneInfo('Europe/Berlin')

def main():
    config=sources();cd=config['znni_alkaline']['current_density_a_dm2']['barrel'][0]
    ripple=config['assumptions']['rectifier_noise']['value'];factor=config['assumptions']['rectifier_fault_factor']['value']
    start=datetime(2026,3,2,tzinfo=TZ);end=datetime(2026,3,9,tzinfo=TZ);fault_day=start+timedelta(days=2)
    rng=random.Random(3);step=10
    with connect('simulate_rectifiers') as conn:
        rows=conn.execute('''SELECT st.step_id,st.station_id,s.code,lower(st.in_station),upper(st.in_station),
          sum(ll.qty_pcs*p.area_dm2)::float8 FROM trace.load_step st JOIN ref.station s USING(station_id)
          JOIN ref.tank t USING(tank_id) JOIN trace.load_lot ll USING(load_id)
          JOIN trace.lot l USING(lot_id) JOIN ref.part p USING(part_id)
          WHERE t.function='plate' AND st.in_station && tstzrange(%s,%s)
          GROUP BY st.step_id,s.code ORDER BY st.station_id,lower(st.in_station)''',(start,end)).fetchall()
        slots=sorted({(r[1],r[2]) for r in rows});faulty=rng.choice(slots)[0];mapping={}
        for sid,code in slots:
            # Sampled every 10 s and at switching boundaries: a value older than 11 s is not trusted.
            signal=conn.execute("INSERT INTO hist.signal(tag,description,unit,origin,source,station_id,acquisition_mode,max_hold) VALUES(%s,'Simulated rectifier output current','A','simulated','sim:config/sources.yaml',%s,'sampled',interval '11 seconds') ON CONFLICT(tag) DO UPDATE SET station_id=EXCLUDED.station_id,acquisition_mode=EXCLUDED.acquisition_mode,max_hold=EXCLUDED.max_hold RETURNING signal_id",('Sim.'+code.replace('-','.')+'.Current',sid)).fetchone()[0];mapping[sid]=signal
            intervals=[r for r in rows if r[1]==sid];starts=[r[3] for r in intervals]
            times={start+timedelta(seconds=i) for i in range(0,int((end-start).total_seconds())+1,step)}
            times.update(t for r in intervals for t in r[3:5] if start<=t<=end)
            conn.execute('CREATE TEMP TABLE incoming_rectifier (LIKE hist.sample INCLUDING ALL) ON COMMIT DROP')
            with conn.cursor().copy('COPY incoming_rectifier FROM STDIN') as copy:
                for t in sorted(times):
                    i=bisect.bisect_right(starts,t)-1;amps=0.
                    if i>=0 and intervals[i][3]<=t<intervals[i][4]:
                        amps=intervals[i][5]*cd*(1+rng.gauss(0,ripple))
                        if sid==faulty and fault_day<=t<fault_day+timedelta(days=1):amps*=factor
                    copy.write_row((signal,t,round(amps,6),0))
            conn.execute('INSERT INTO hist.sample SELECT * FROM incoming_rectifier ON CONFLICT(signal_id,ts) DO UPDATE SET value=EXCLUDED.value WHERE hist.sample.value IS DISTINCT FROM EXCLUDED.value')
            conn.execute('DROP TABLE incoming_rectifier')
        for step_id,sid,code,a,b,area in rows:
            if a<start or b>end:continue
            result=conn.execute("SELECT * FROM hist.integral(%s,tstzrange(%s,%s),interval '11 seconds','hold')",(mapping[sid],a,b)).fetchone()
            ah,covered,requested=result;assert abs(covered-requested)<1e-5
            conn.execute('INSERT INTO hist.step_energy VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(step_id) DO UPDATE SET measured_ah=EXCLUDED.measured_ah,coverage=EXCLUDED.coverage',
                (step_id,mapping[sid],area*cd*(b-a).total_seconds()/3600,ah,covered/requested,'hold; exact switching boundaries',sid==faulty and a<fault_day+timedelta(days=1) and b>fault_day))
            conn.execute('UPDATE trace.load_step SET amp_hours=%s WHERE step_id=%s AND amp_hours IS DISTINCT FROM %s::numeric(12,3)',(ah,step_id,ah))
        anomalies=conn.execute('SELECT step_id,station,started,ratio,fault_expected FROM hist.v_ah_ratio WHERE ratio NOT BETWEEN .97 AND 1.03 ORDER BY started').fetchall()
        report={'start':str(start),'end':str(end),'step_seconds':step,'slots':len(slots),'fault_station_id':faulty,'fault_day':str(fault_day.date()),'fault_factor':factor,
          'carrier_steps':conn.execute('SELECT count(*) FROM hist.step_energy').fetchone()[0],
          'samples':conn.execute("SELECT count(*) FROM hist.sample JOIN hist.signal USING(signal_id) WHERE origin='simulated' AND station_id IS NOT NULL").fetchone()[0],
          'flagged_steps':[dict(zip(['step_id','station','started','ratio','fault_expected'],r)) for r in anomalies]}
        assert anomalies and all(r[-1] for r in anomalies)
    # Backfilled history is outside the aggregate policy's 30-day window: refresh exactly this week.
    refresh_range(start,end)
    (ROOT/'docs/results/rectifiers.json').write_text(json.dumps(report,default=str,indent=2),encoding='utf-8');print(json.dumps(report,default=str,indent=2))
if __name__=='__main__':main()
