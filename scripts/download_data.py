"""Pinned SKAB source files and original UCI archive, with SHA-256 provenance."""
import hashlib,json,pathlib,urllib.request,zipfile
from datetime import datetime,timezone
ROOT=pathlib.Path(__file__).resolve().parents[1]
COMMIT='b2c0d46c2971dcbfe71e26087b6d231998bb91c2'
BASE=f'https://raw.githubusercontent.com/waico/SKAB/{COMMIT}/'
FILES={f'SKAB/{f}':BASE+f for f in ['data/anomaly-free/anomaly-free.csv','data/valve1/1.csv','data/valve1/2.csv','data/valve2/1.csv','README.md','LICENSE']}
FILES['steel/steel.zip']='https://archive.ics.uci.edu/static/public/851/steel+industry+energy+consumption.zip'

def main():
    manifest=[]
    for name,url in FILES.items():
        path=ROOT/'data/raw'/name;path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            req=urllib.request.Request(url,headers={'User-Agent':'PartecHistorianPortfolio/1.0'})
            with urllib.request.urlopen(req,timeout=60) as r:content=r.read()
            path.write_bytes(content)
        manifest.append({'path':'data/raw/'+name,'url':url,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    with zipfile.ZipFile(ROOT/'data/raw/steel/steel.zip') as archive:
        name=next(n for n in archive.namelist() if n.endswith('Steel_industry_data.csv'))
        content=archive.read(name);(ROOT/'data/raw/steel/Steel_industry_data.csv').write_bytes(content)
        manifest.append({'path':'data/raw/steel/Steel_industry_data.csv','archive_member':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
    out=ROOT/'docs/results';out.mkdir(parents=True,exist_ok=True)
    (out/'download_manifest.json').write_text(json.dumps({'recorded_at':datetime.now(timezone.utc).isoformat(),'skab_commit':COMMIT,'files':manifest},indent=2),encoding='utf-8')
    print(f'Downloaded/verified {len(manifest)} files')
if __name__=='__main__':main()
