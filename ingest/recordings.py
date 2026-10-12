"""Source parsing and metadata, with source-local clocks explicitly assigned UTC."""
import csv,math
from datetime import datetime,timezone
from common import ROOT,connect
SKAB={'Accelerometer1RMS':'g','Accelerometer2RMS':'g','Current':'A','Pressure':'bar','Temperature':'degC','Thermocouple':'degC','Voltage':'V','Volume Flow RateRMS':'L/min','anomaly':'flag','changepoint':'flag'}
STEEL={'Usage_kWh':'kWh','Lagging_Current_Power_Factor':'%'}
FILES=['valve1/1.csv','valve1/2.csv','valve2/1.csv']

def load_skab(relative):
    path=ROOT/'data/raw/SKAB/data'/relative
    with path.open(newline='',encoding='utf-8-sig') as f:rows=list(csv.DictReader(f,delimiter=';'))
    for row in rows:
        row['ts']=datetime.strptime(row['datetime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        for col in SKAB:
            if col in row:
                row[col]=float(row[col]);assert math.isfinite(row[col])
        for col in ('anomaly','changepoint'):
            if col in row:assert row[col] in (0,1)
    assert rows and all(a['ts']<b['ts'] for a,b in zip(rows,rows[1:])),relative
    return rows

def load_steel():
    with (ROOT/'data/raw/steel/Steel_industry_data.csv').open(newline='',encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    assert len(rows)==35040
    for row in rows:
        assert all(v!='' for v in row.values())
        row['ts']=datetime.strptime(row['date'],'%d/%m/%Y %H:%M').replace(tzinfo=timezone.utc)
        for col in STEEL:row[col]=float(row[col]);assert math.isfinite(row[col])
    # Original file puts each date's 00:00 row after its 23:45 row. Preserve the
    # literal timestamps and sort them; do not silently relabel midnight as tomorrow.
    rows.sort(key=lambda r:r['ts'])
    assert all((b['ts']-a['ts']).total_seconds()==900 for a,b in zip(rows,rows[1:]))
    return rows

def recording_id(relative):return relative.replace('/','_').replace('.csv','')

def register_recordings():
    import statistics
    baseline=load_skab('anomaly-free/anomaly-free.csv');values=[r['Current'] for r in baseline]
    mean=statistics.mean(values);sd=statistics.stdev(values)
    with connect('register_recordings') as conn:
        conn.execute("INSERT INTO hist.baseline VALUES('Current',%s,%s,%s,%s,%s,%s) ON CONFLICT(parameter) DO UPDATE SET n=EXCLUDED.n,mean=EXCLUDED.mean,sd=EXCLUDED.sd,low_limit=EXCLUDED.low_limit,high_limit=EXCLUDED.high_limit",(len(values),mean,sd,mean-3*sd,mean+3*sd,'skab:anomaly-free/anomaly-free.csv'))
        for relative in FILES:
            rows=load_skab(relative);rid=recording_id(relative)
            conn.execute('INSERT INTO hist.recording VALUES(%s,%s,\'real\',%s,%s,%s,%s) ON CONFLICT DO NOTHING',(rid,'skab:'+relative,rows[0]['ts'],rows[-1]['ts'],len(rows),'Source timezone absent; wall clock assigned UTC without clock conversion'))
            with conn.cursor() as cur:cur.executemany('INSERT INTO hist.recording_point VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',[(rid,r['ts'],bool(r['anomaly'])) for r in rows])
        rows=load_steel()
        conn.execute("INSERT INTO hist.recording VALUES('steel2018','uci:851','real',%s,%s,%s,%s) ON CONFLICT DO NOTHING",(rows[0]['ts'],rows[-1]['ts'],len(rows),'Source timezone absent; wall clock assigned UTC without clock conversion'))

if __name__=='__main__':register_recordings()
