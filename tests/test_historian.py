from collections import Counter
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import asyncio,statistics
from zoneinfo import ZoneInfo
import pytest
from asyncua import ua
from opcua_collector import Handler,flush,mark_unvouched
from recordings import load_skab,load_steel

def series(db,values,qualities=None):
    sid=db.execute("INSERT INTO hist.signal(tag,origin,source,unit) VALUES('test.current','simulated','pytest','A') RETURNING signal_id").fetchone()[0]
    start=datetime(2025,1,1,tzinfo=timezone.utc)
    with db.cursor() as cur:cur.executemany('INSERT INTO hist.sample VALUES(%s,%s,%s,%s)',[(sid,start+timedelta(seconds=t),v,(qualities or {}).get(t,0)) for t,v in values])
    return sid,start

@pytest.mark.parametrize('method,expected',[('linear',.5),('hold',0.)])
def test_exact_boundary_integration(db,method,expected):
    sid,start=series(db,[(0,0),(3600,2)])
    result=db.execute('SELECT hist.integrate_hours(%s,tstzrange(%s,%s),interval \'2 hours\',%s)',(sid,start+timedelta(minutes=15),start+timedelta(minutes=45),method)).fetchone()[0]
    assert result==pytest.approx(expected)

def test_missing_tail_is_null_not_zero(db):
    sid,start=series(db,[(0,10),(10,10)])
    assert db.execute("SELECT hist.integrate_hours(%s,tstzrange(%s,%s))",(sid,start,start+timedelta(seconds=20))).fetchone()[0] is None

def test_excessive_gap_excluded(db):
    sid,start=series(db,[(0,10),(60,10)])
    ah,covered,total=db.execute('SELECT * FROM hist.integral(%s,tstzrange(%s,%s))',(sid,start,start+timedelta(seconds=60))).fetchone()
    assert ah is None and covered==0 and total==60

def test_uncertain_segment_excluded(db):
    sid,start=series(db,[(0,10),(10,10),(20,10)],{10:1})
    assert db.execute('SELECT hist.integrate_hours(%s,tstzrange(%s,%s))',(sid,start,start+timedelta(seconds=20))).fetchone()[0] is None

def test_zero_current_is_valid_zero(db):
    sid,start=series(db,[(0,0),(10,0)])
    assert db.execute('SELECT hist.integrate_hours(%s,tstzrange(%s,%s))',(sid,start,start+timedelta(seconds=10))).fetchone()[0]==0

def test_excursions_support_one_sided_limits_and_break_gaps(db):
    sid,start=series(db,[(0,3),(1,3),(20,3),(21,0),(22,3)])
    db.execute('UPDATE hist.signal SET high_limit=2 WHERE signal_id=%s',(sid,))
    assert sorted(r[0] for r in db.execute('SELECT samples FROM hist.v_excursion WHERE signal_id=%s',(sid,)))==[1,1,2]

def test_baseline_from_independent_source(db):
    values=[r['Current'] for r in load_skab('anomaly-free/anomaly-free.csv')]
    row=db.execute('SELECT n,mean,sd,low_limit,high_limit FROM hist.baseline').fetchone()
    assert row[0]==len(values) and row[1]==pytest.approx(statistics.mean(values))
    assert row[3]==pytest.approx(statistics.mean(values)-3*statistics.stdev(values))

def test_sql_confusion_matrix_matches_independent_raw_scoring(db):
    low,high=db.execute('SELECT low_limit,high_limit FROM hist.baseline').fetchone()
    for rid,path in [('valve1_1','valve1/1.csv'),('valve1_2','valve1/2.csv'),('valve2_1','valve2/1.csv')]:
        data=load_skab(path);pairs=[(r['Current']<low or r['Current']>high,bool(r['anomaly'])) for r in data]
        expected=tuple(sum(p==a and actual==b for p,actual in pairs) for a,b in [(True,True),(True,False),(False,True),(False,False)])
        row=db.execute('SELECT tp,fp,fn,tn,total,scored FROM hist.v_scores WHERE recording_id=%s',(rid,)).fetchone()
        assert row[:4]==expected and row[4]==row[5]==len(data)

def test_steel_sample_hold_reproduces_all_intervals(db):
    raw=load_steel();actual=db.execute('SELECT ts,usage_kwh FROM hist.v_steel_grid ORDER BY ts').fetchall()
    assert actual==[(r['ts'],r['Usage_kWh']) for r in raw]
    assert db.execute('SELECT sum(energy_kwh),min(intervals),max(intervals) FROM hist.v_steel_daily').fetchone()==pytest.approx((sum(r['Usage_kWh'] for r in raw),96,96))

def test_fault_query_finds_only_planted_carriers(db):
    rows=db.execute('SELECT ratio,fault_expected,coverage FROM hist.v_ah_ratio').fetchall()
    assert len(rows)==486 and all(r[2]==1 for r in rows)
    assert sum(r[1] for r in rows)==3
    assert all((.97<=ratio<=1.03)!=fault for ratio,fault,_ in rows)
    assert all(.69<ratio<.71 for ratio,fault,_ in rows if fault)

def test_lab_reference_comparison(db):
    rows=db.execute('SELECT day,difference_pct,coverage FROM hist.v_lab_comparison WHERE lab_reference_ah>0 ORDER BY day').fetchall()
    assert len(rows)==5
    assert all(coverage==1 for _,_,coverage in rows)
    assert all(abs(pct)<.1 for day,pct,_ in rows if day.day!=4)
    assert next(pct for day,pct,_ in rows if day.day==4)<-.1

def test_timescale_is_real_and_aggregate_agrees(db):
    assert db.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'").fetchone()
    assert db.execute("SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema='hist' AND hypertable_name='sample'").fetchone()
    assert db.execute('SELECT sum(n) FROM hist.sample_1h').fetchone()[0]==db.execute('SELECT count(*) FROM hist.sample').fetchone()[0]

def test_readonly_role_cannot_write(db):
    import psycopg
    db.execute('SET LOCAL ROLE role_readonly')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('UPDATE hist.sample SET value=0')

@pytest.mark.parametrize('value,status,ts,expected',[
 (0,ua.StatusCodes.BadWaitingForInitialData,True,'bad_skipped'),
 (float('nan'),ua.StatusCodes.Good,True,'nonnumeric_skipped'),
 ('abc',ua.StatusCodes.Good,True,'nonnumeric_skipped'),
 (1,ua.StatusCodes.Good,False,'missing_source_timestamp'),
 (1,ua.StatusCodes.Good,True,'received'),
 (1,ua.StatusCodes.Uncertain,True,'received')])
def test_collector_quality(value,status,ts,expected):
    queue=asyncio.Queue();node=SimpleNamespace(nodeid='test');handler=Handler(queue,{'test':1})
    dv=ua.DataValue(ua.Variant(value),StatusCode=ua.StatusCode(status),SourceTimestamp=datetime.now(timezone.utc) if ts else None)
    handler.datachange_notification(node,value,SimpleNamespace(monitored_item=SimpleNamespace(Value=dv)))
    assert handler.stats[expected]==1
    if status==ua.StatusCodes.Uncertain:assert queue.get_nowait()[4]==1

def test_queue_overflow_is_visible():
    q=asyncio.Queue(maxsize=1);h=Handler(q,{'test':1});node=SimpleNamespace(nodeid='test')
    dv=ua.DataValue(ua.Variant(1.),SourceTimestamp=datetime.now(timezone.utc));data=SimpleNamespace(monitored_item=SimpleNamespace(Value=dv))
    h.datachange_notification(node,1.,data);h.datachange_notification(node,1.,data)
    assert isinstance(h.failure,RuntimeError)

def test_collector_conflicting_duplicate_is_quarantined_and_batch_commits(db):
    sid,t=series(db,[(0,10)])
    other=db.execute("INSERT INTO hist.signal(tag,origin,source) VALUES('test.other','simulated','pytest') RETURNING signal_id").fetchone()[0]
    result=flush(db,[(sid,t,11.,0),(other,t,1.,0),(other,t+timedelta(seconds=1),2.,0),(other,t+timedelta(seconds=1),3.,0)])
    assert result['inserted']==2 and result['quarantined']==2
    assert db.execute('SELECT value FROM hist.sample WHERE signal_id=%s',(sid,)).fetchone()[0]==10
    assert [r[0] for r in db.execute('SELECT value FROM hist.sample WHERE signal_id=%s ORDER BY ts',(other,))]==[1,2]
    assert sorted(db.execute('SELECT value,kept_value FROM hist.sample_quarantine WHERE signal_id IN (%s,%s)',(sid,other)).fetchall())==[(3,2),(11,10)]

def test_double_conflict_keeps_the_recorded_value(db):
    sid,t=series(db,[(0,10)])
    assert flush(db,[(sid,t,11.,0),(sid,t,12.,0)])['quarantined']==2
    assert sorted(db.execute('SELECT value,kept_value FROM hist.sample_quarantine WHERE signal_id=%s',(sid,)).fetchall())==[(11,10),(12,10)]

def test_collector_duplicate_is_idempotent(db):
    sid,t=series(db,[(0,10)])
    assert flush(db,[(sid,t,10.,0)])['inserted']==0

# Acquisition gaps, hold age and the collector's gap markers (F-08, F-09)
def steel_day(db,day):
    return db.execute('SELECT energy_kwh,intervals,good_intervals,coverage FROM hist.v_steel_daily WHERE source_date=%s',(day,)).fetchone()

def test_steel_day_with_gap_marker_is_incomplete(db):
    complete=steel_day(db,'2018-06-01');assert complete[1:]==(96,96,1)
    sid=db.execute("SELECT signal_id FROM hist.signal WHERE tag='SteelPlant.MainMeter.Usage_kWh'").fetchone()[0]
    a,b=db.execute("SELECT min(ts),max(ts) FROM hist.sample WHERE signal_id=%s AND ts BETWEEN '2018-06-01 06:00Z' AND '2018-06-01 10:00Z'",(sid,)).fetchone()
    lost=db.execute('DELETE FROM hist.sample WHERE signal_id=%s AND ts>%s AND ts<%s',(sid,a,b)).rowcount
    assert lost>0 and steel_day(db,'2018-06-01')[1:]==(96,96,1) # silent loss shorter than max_hold (6 h) looks complete
    db.execute("INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,'pytest')",(sid,a,b))
    energy,intervals,good,coverage=steel_day(db,'2018-06-01')
    assert energy is None and intervals==96 and good<96 and coverage<1

def test_steel_hold_never_exceeds_max_hold(db):
    sid=db.execute("SELECT signal_id FROM hist.signal WHERE tag='SteelPlant.MainMeter.Usage_kWh'").fetchone()[0]
    db.execute("DELETE FROM hist.sample WHERE signal_id=%s AND ts>'2018-06-01 00:00Z' AND ts<'2018-06-01 23:45Z'",(sid,))
    energy,intervals,good,coverage=steel_day(db,'2018-06-01')
    assert energy is None and intervals==96 and good<=26 and coverage<1 # held for at most 6 h

def test_scoring_stops_at_gap_marker(db):
    sid=db.execute("SELECT signal_id FROM hist.signal WHERE tag='SKAB.valve1_1.WaterLoop.Current'").fetchone()[0]
    a=db.execute("SELECT ts FROM hist.sample WHERE signal_id=%s ORDER BY ts OFFSET 100 LIMIT 1",(sid,)).fetchone()[0]
    b=db.execute("SELECT ts FROM hist.sample WHERE signal_id=%s ORDER BY ts OFFSET 386 LIMIT 1",(sid,)).fetchone()[0]
    db.execute('DELETE FROM hist.sample WHERE signal_id=%s AND ts>%s AND ts<%s',(sid,a,b))
    db.execute("INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,'pytest')",(sid,a,b))
    total,scored=db.execute("SELECT total,scored FROM hist.v_scores WHERE recording_id='valve1_1'").fetchone()
    assert total==1145 and scored==1145-285

def test_scores_report_chance_baselines(db):
    rows={r[0]:r[1:] for r in db.execute('SELECT recording_id,prevalence,fpr,tpr_minus_fpr,f1,always_anomaly_f1 FROM hist.v_scores')}
    assert rows['valve1_1'][:3]==pytest.approx((402/1145,315/743,201/402-315/743))
    assert all(f1<always for _,_,_,f1,always in rows.values()) # no skill on these recordings

def test_integration_respects_gap_marker(db):
    sid,start=series(db,[(t,2) for t in range(0,61,10)])
    window=(sid,start,start+timedelta(seconds=60))
    assert db.execute('SELECT hist.integrate_hours(%s,tstzrange(%s,%s))',window).fetchone()[0]==pytest.approx(2*60/3600)
    db.execute("INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,'pytest')",(sid,start+timedelta(seconds=25),start+timedelta(seconds=27)))
    assert db.execute('SELECT hist.integrate_hours(%s,tstzrange(%s,%s))',window).fetchone()[0] is None

def test_lab_comparison_needs_full_coverage(db):
    station,tank=db.execute("SELECT g.station_id,t.code FROM hist.signal g JOIN ref.station s USING(station_id) JOIN ref.tank t USING(tank_id) LIMIT 1").fetchone()
    sid=db.execute("INSERT INTO hist.signal(tag,origin,source,unit,station_id,acquisition_mode,max_hold) VALUES('test.rectifier','simulated','pytest','A',%s,'sampled','11 seconds') RETURNING signal_id",(station,)).fetchone()[0]
    day=datetime(2025,1,1,tzinfo=ZoneInfo('Europe/Berlin'))
    with db.cursor() as cur:cur.executemany('INSERT INTO hist.sample VALUES(%s,%s,1,0)',[(sid,day+timedelta(seconds=t)) for t in range(0,43201,10)]+[(sid,day+timedelta(days=1,seconds=5))])
    db.execute("INSERT INTO hist.lab_charge_reference VALUES(%s,'2025-01-01',6,'pytest')",(tank,))
    hist,coverage,diff,pct=db.execute("SELECT historian_ah,coverage,difference_ah,difference_pct FROM hist.v_lab_comparison WHERE tank_code=%s AND day='2025-01-01'",(tank,)).fetchone()
    assert coverage==pytest.approx(.5) and hist==pytest.approx(12) and diff is None and pct is None

def notify(handler,value,seconds,status=ua.StatusCodes.Good,ts=True):
    dv=ua.DataValue(ua.Variant(value),StatusCode=ua.StatusCode(status),SourceTimestamp=datetime(2030,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds) if ts else None)
    handler.datachange_notification(SimpleNamespace(nodeid='n'),value,SimpleNamespace(monitored_item=SimpleNamespace(Value=dv)))

def drained(queue):
    items=[]
    while not queue.empty():items.append(queue.get_nowait())
    return items

def test_bad_status_opens_gap_and_next_value_closes_it():
    q=asyncio.Queue();h=Handler(q,{'n':7})
    notify(h,1.,1);notify(h,0.,2,ua.StatusCodes.BadSensorFailure);notify(h,float('nan'),3);notify(h,4.,5)
    t=lambda s:datetime(2030,1,1,tzinfo=timezone.utc)+timedelta(seconds=s)
    assert [i for i in drained(q) if i[0]!='sample']==[('open',7,t(2),'bad status'),('close',7,t(2),t(5))]
    notify(h,0.,9,ua.StatusCodes.BadWaitingForInitialData);assert drained(q)==[] # no value yet loses nothing

def test_server_queue_overflow_bit_is_counted_and_marked():
    q=asyncio.Queue();h=Handler(q,{'n':7})
    notify(h,1.,1);notify(h,2.,50,ua.StatusCodes.Good|0x0480) # InfoType DataValue + Overflow
    t=lambda s:datetime(2030,1,1,tzinfo=timezone.utc)+timedelta(seconds=s)
    assert h.stats['server_queue_overflow']==1 and ('gap',7,t(1),t(50),'server queue overflow') in drained(q)

def test_restart_closes_stop_gap_with_unchanged_value():
    t=lambda s:datetime(2030,1,1,tzinfo=timezone.utc)+timedelta(seconds=s)
    q=asyncio.Queue();h=Handler(q,{'n':7},{7:[t(100)]},{7:[t(100)]})
    notify(h,1.,40) # first notification after restart: current value, last changed before the stop
    assert ('drop',7,t(100)) in drained(q)
    q=asyncio.Queue();h=Handler(q,{'n':7},{7:[t(100)]},{})
    notify(h,1.,40);assert [i for i in drained(q) if i[0]!='sample']==[] # an outage gap needs a newer value

def test_queue_overflow_still_records_the_failure_gap(db):
    sid,_=series(db,[(0,10)]);q=asyncio.Queue(maxsize=2);h=Handler(q,{'n':sid})
    notify(h,1.,1);notify(h,2.,2);notify(h,3.,3) # the third value does not fit: the queue stays full
    assert isinstance(h.failure,RuntimeError)
    asyncio.run(mark_unvouched(db,q,Counter(),h,[sid],'collector failed'))
    t=lambda s:datetime(2030,1,1,tzinfo=timezone.utc)+timedelta(seconds=s)
    assert db.execute('SELECT count(*) FROM hist.sample WHERE signal_id=%s AND ts>=%s',(sid,t(1))).fetchone()[0]==2
    assert db.execute('SELECT from_ts,to_ts,reason FROM hist.acquisition_gap WHERE signal_id=%s',(sid,)).fetchall()==[(t(2),None,'collector failed')]

def test_bad_status_point_is_not_scored_with_older_value(db):
    sid=db.execute("SELECT signal_id FROM hist.signal WHERE tag='SKAB.valve1_1.WaterLoop.Current'").fetchone()[0]
    a=db.execute("SELECT ts FROM hist.sample WHERE signal_id=%s ORDER BY ts OFFSET 100 LIMIT 1",(sid,)).fetchone()[0]
    db.execute('DELETE FROM hist.sample WHERE signal_id=%s AND ts=%s',(sid,a)) # the value at a was bad and not stored
    db.execute("INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,'bad status')",(sid,a,a+timedelta(seconds=1)))
    assert db.execute("SELECT current_a,predicted FROM hist.v_skab_scored WHERE recording_id='valve1_1' AND ts=%s",(a,)).fetchone()==(None,None)

def test_excursion_breaks_at_gap_marker(db):
    sid,start=series(db,[(0,3),(1,3),(2,3)])
    db.execute('UPDATE hist.signal SET high_limit=2 WHERE signal_id=%s',(sid,))
    db.execute("INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,'pytest')",(sid,start+timedelta(seconds=1),start+timedelta(seconds=2)))
    assert sorted(r[0] for r in db.execute('SELECT samples FROM hist.v_excursion WHERE signal_id=%s',(sid,)))==[1,2]

def test_gap_markers_are_stored(db):
    sid,t=series(db,[(0,10)])
    gaps=[('open',sid,t,'connection lost'),('close',sid,t,t+timedelta(seconds=30)),('gap',sid,None,t+timedelta(seconds=60),'server queue overflow')]
    assert flush(db,[(sid,t+timedelta(seconds=30),11.,0)],gaps)['gaps_recorded']==2
    assert db.execute('SELECT from_ts,to_ts,reason FROM hist.acquisition_gap WHERE signal_id=%s ORDER BY gap_id',(sid,)).fetchall()==[
     (t,t+timedelta(seconds=30),'connection lost'),(t+timedelta(seconds=30),t+timedelta(seconds=60),'server queue overflow')]

# Permissions (F-01, F-34)
def test_lab_roles_cannot_rewrite_analyses(db):
    import psycopg
    db.execute('SET LOCAL ROLE role_lab')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('UPDATE lab.analysis SET sampled_at=sampled_at')

def test_readonly_cannot_attach_definer_trigger(db):
    import psycopg
    db.execute('SET LOCAL ROLE role_readonly')
    db.execute('CREATE TEMP TABLE probe(tank_id int,sampled_at timestamptz,valid_during tstzrange)')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('CREATE TRIGGER t BEFORE INSERT ON probe FOR EACH ROW EXECUTE FUNCTION lab.set_analysis_validity()')

def test_charge_writes_are_audited(db):
    step=db.execute('SELECT step_id FROM hist.step_energy ORDER BY step_id LIMIT 1').fetchone()[0]
    db.execute("SELECT set_config('app.user','simulate_rectifiers',true)")
    new=db.execute('UPDATE trace.load_step SET amp_hours=coalesce(amp_hours,0)+1 WHERE step_id=%s RETURNING amp_hours',(step,)).fetchone()[0]
    row=db.execute("SELECT changed_by,login_user,(new_row->>'amp_hours')::numeric FROM audit.change_log WHERE table_name='trace.load_step' AND row_pk->>'step_id'=%s ORDER BY change_id DESC LIMIT 1",(str(step),)).fetchone()
    assert row==('simulate_rectifiers',db.execute('SELECT session_user').fetchone()[0],new)

def test_dashboard_role_reads_panels_only(db):
    import json,re,psycopg
    from conftest import ROOT
    panels=json.loads((ROOT/'grafana/provisioning/dashboards/line-historian.json').read_text(encoding='utf-8'))['panels']
    db.execute('SET LOCAL ROLE role_dashboard')
    for panel in panels: # same source periods as scripts/verify_grafana.py
        a,b=('2018-01-01','2019-01-01') if panel['id']==6 else ('2020-03-09 10:34Z','2020-03-09 10:55Z') if panel['id']==2 else ('2026-03-01 23:00Z','2026-03-08 23:00Z')
        query=re.sub(r"\$__timeFilter\(([^()]*(?:\([^()]*\))?[^()]*)\)",lambda m:f"{m.group(1)} BETWEEN '{a}' AND '{b}'",panel['targets'][0]['rawSql'])
        assert db.execute(query).fetchall(),panel['title']
    db.execute('SAVEPOINT denied')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('SELECT count(*) FROM trace.lot')
    db.execute('ROLLBACK TO SAVEPOINT denied')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('SELECT count(*) FROM hist.recording_point')

# Continuous aggregate after late history (F-33)
def test_late_history_needs_range_refresh():
    from common import connect
    from timescale import refresh_range
    c=connect('pytest');c.commit();c.autocommit=True
    agg="SELECT coalesce(sum(n),0) FROM hist.sample_1h WHERE bucket='2018-06-01 12:00Z'"
    raw="SELECT count(*) FROM hist.sample WHERE ts>='2018-06-01 12:00Z' AND ts<'2018-06-01 13:00Z'"
    sid=c.execute("INSERT INTO hist.signal(tag,origin,source) VALUES('test.late','simulated','pytest') RETURNING signal_id").fetchone()[0]
    try:
        before=c.execute(agg).fetchone()[0];assert before==c.execute(raw).fetchone()[0]
        c.execute("INSERT INTO hist.sample VALUES(%s,'2018-06-01 12:00:07Z',1,0)",(sid,))
        assert c.execute(agg).fetchone()[0]==before # older than the policy window: stale until refreshed
        refresh_range(datetime(2018,6,1,12,0,7,tzinfo=timezone.utc),datetime(2018,6,1,12,0,7,tzinfo=timezone.utc))
        assert c.execute(agg).fetchone()[0]==before+1==c.execute(raw).fetchone()[0]
    finally:
        c.execute('DELETE FROM hist.sample WHERE signal_id=%s',(sid,));c.execute('DELETE FROM hist.signal WHERE signal_id=%s',(sid,))
        refresh_range(datetime(2018,6,1,12,tzinfo=timezone.utc),datetime(2018,6,1,12,tzinfo=timezone.utc));c.close()
