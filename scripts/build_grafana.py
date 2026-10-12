"""Provision a database-only Grafana dashboard, with explicit source-period links."""
import json,pathlib
from datetime import datetime
ROOT=pathlib.Path(__file__).resolve().parents[1]
DS={'type':'grafana-postgresql-datasource','uid':'historian-postgres'}

def main():
    def epoch(value):return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)
    panels=[]
    def panel(title,query,x,y,w,h,kind='timeseries',unit=None):
        p={'id':len(panels)+1,'title':title,'type':kind,'datasource':DS,'gridPos':{'x':x,'y':y,'w':w,'h':h},
          'targets':[{'refId':'A','datasource':DS,'rawSql':query,'format':'time_series' if kind=='timeseries' else 'table','editorMode':'code','rawQuery':True}],
          'fieldConfig':{'defaults':{'custom':{'lineWidth':1,'fillOpacity':8},'unit':unit or 'short'},'overrides':[]},
          'options':{'legend':{'displayMode':'list','placement':'bottom'},'tooltip':{'mode':'multi'}} if kind=='timeseries' else {'showHeader':True}}
        panels.append(p)
    panel('Rectifier current · simulated · use Rectifier week link',"SELECT time_bucket('5 minutes',s.ts) AS time,g.tag AS metric,avg(s.value) AS value FROM hist.sample s JOIN hist.signal g USING(signal_id) WHERE g.station_id IS NOT NULL AND $__timeFilter(s.ts) GROUP BY 1,2 ORDER BY 1,2",0,0,24,10,unit='amp')
    panel('SKAB pump current and recorded anomaly · use SKAB link',"SELECT p.ts AS time,p.current_a AS current_a,p.anomaly::int AS anomaly FROM hist.v_skab_scored p WHERE recording_id='valve1_1' AND $__timeFilter(p.ts) ORDER BY 1",0,10,14,9,unit='amp')
    panels[-1]['fieldConfig']['overrides']=[{'matcher':{'id':'byName','options':'anomaly'},'properties':[{'id':'unit','value':'short'},{'id':'custom.axisPlacement','value':'right'},{'id':'custom.drawStyle','value':'bars'},{'id':'min','value':0},{'id':'max','value':1}]}]
    panel('Carrier charge · measured / planned · simulated',"SELECT station,carrier_no,round(ratio::numeric,4) AS ratio,round(measured_ah::numeric,3) AS ah,round(planned_ah::numeric,3) AS planned_ah,started,coverage,fault_expected FROM hist.v_ah_ratio ORDER BY ratio,started LIMIT 100",14,10,10,9,'table')
    panel('SKAB detector score · independent anomaly-free baseline',"SELECT * FROM hist.v_scores ORDER BY recording_id",0,19,12,7,'table')
    panel('Daily charge versus Project 2 · simulated',"SELECT day,round(historian_ah::numeric,2) AS historian_ah,round(lab_reference_ah,2) AS lab_reference_ah,round(difference_pct::numeric,3) AS difference_pct,coverage FROM hist.v_lab_comparison ORDER BY day",12,19,12,7,'table')
    panel('Steel plant daily energy · real · use Steel 2018 link',"SELECT source_date::timestamp AT TIME ZONE 'UTC' AS time,energy_kwh AS value FROM hist.v_steel_daily WHERE $__timeFilter(source_date::timestamp AT TIME ZONE 'UTC') ORDER BY 1",0,26,24,8,unit='kwatth')
    panels[-1]['description']='Sum of 96 reconstructed 15-minute readings per literal source date. Repeated unchanged values are carried forward before summation. Source timezone is unknown; no midnight date correction is inferred.'
    dashboard={'uid':'partec-line-historian','title':'Partec · line historian','schemaVersion':39,'version':1,'timezone':'utc','editable':True,
      'tags':['portfolio','OPC UA','TimescaleDB'],'time':{'from':'2026-03-01T23:00:00Z','to':'2026-03-08T23:00:00Z'},'refresh':'',
      'description':'Real SKAB/steel timestamps and simulated 2026 rectifier timestamps are separate. Choose a source-period link; other time-series panels may be empty. Tables summarize the loaded datasets.',
      'links':[{'title':label,'type':'link','url':f'/d/partec-line-historian?from={epoch(a)}&to={epoch(b)}','targetBlank':False} for label,a,b in [
       ('Rectifier week · simulated','2026-03-01T23:00:00Z','2026-03-08T23:00:00Z'),('SKAB · real pump','2020-03-09T10:34:00Z','2020-03-09T10:55:00Z'),('Steel 2018 · real meter','2018-01-01T00:00:00Z','2019-01-01T00:00:00Z')]],'panels':panels}
    dest=ROOT/'grafana/provisioning/dashboards';dest.mkdir(parents=True,exist_ok=True)
    (dest/'line-historian.json').write_text(json.dumps(dashboard,indent=2),encoding='utf-8')
    (dest/'provider.yml').write_text('apiVersion: 1\nproviders:\n  - name: Historian\n    type: file\n    disableDeletion: true\n    options:\n      path: ${HIST_DASHBOARDS_PATH}\n',encoding='utf-8')
    (ROOT/'grafana/provisioning/datasources/postgres.yml').write_text('''apiVersion: 1
datasources:
  - name: Historian PostgreSQL
    uid: historian-postgres
    type: grafana-postgresql-datasource
    access: proxy
    url: ${HIST_DB_HOST}
    user: ${HIST_DB_USER}
    secureJsonData:
      password: ${HIST_DB_PASSWORD}
    jsonData:
      database: ${HIST_DB_NAME}
      sslmode: disable
      postgresVersion: 1700
      timescaledb: true
    isDefault: true
    editable: false
''',encoding='utf-8')
    print('Grafana dashboard and provisioning files generated')
if __name__=='__main__':main()
