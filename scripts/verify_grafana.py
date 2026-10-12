"""Exercise the provisioned dashboard via Grafana's actual PostgreSQL query API."""
import json,pathlib,urllib.request
from datetime import datetime
ROOT=pathlib.Path(__file__).resolve().parents[1]

def main():
    base='http://127.0.0.1:3003'
    def call(path,payload=None):
        req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
    health=call('/api/health');dashboard=call('/api/dashboards/uid/partec-line-historian')['dashboard']
    report={'health':health,'dashboard_title':dashboard['title'],'panels':[],'browser_render_verified':False}
    for panel in dashboard['panels']:
        period=('2018-01-01T00:00:00Z','2019-01-01T00:00:00Z') if panel['id']==6 else ('2020-03-09T10:34:00Z','2020-03-09T10:55:00Z') if panel['id']==2 else ('2026-03-01T23:00:00Z','2026-03-08T23:00:00Z')
        a,b=[str(int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)) for s in period]
        q={**panel['targets'][0],'intervalMs':300000,'maxDataPoints':2000}
        result=call('/api/ds/query',{'queries':[q],'from':a,'to':b})['results']['A']
        frames=result.get('frames',[]);rows=sum(len(f.get('data',{}).get('values',[[]])[0]) for f in frames)
        assert not result.get('error') and rows>0,(panel['title'],result)
        report['panels'].append({'title':panel['title'],'rows':rows,'status':result.get('status',200)})
    (ROOT/'docs/results/grafana_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
