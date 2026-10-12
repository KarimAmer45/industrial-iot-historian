"""Capture Project 2's simulated daily-charge export as a portable comparison input."""
import argparse,csv,hashlib,json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--csv',type=pathlib.Path);args=ap.parse_args()
    target=ROOT/'config/lab_charge_reference.csv'
    if args.csv:
        with args.csv.open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
        unique={}
        for row in rows:
            key=(row['tank_code'],row['day']);value=row['amp_hours']
            if key in unique and unique[key]!=value:raise ValueError('Conflicting daily totals')
            unique[key]=value
        with target.open('w',newline='',encoding='utf-8') as f:
            writer=csv.writer(f);writer.writerow(['tank_code','day','amp_hours','source'])
            writer.writerows([(*k,v,'Project 2 simulated daily charge; dosing.csv snapshot') for k,v in sorted(unique.items())])
        (ROOT/'docs/results/lab_reference_provenance.json').write_text(json.dumps({'source_file':args.csv.name,'sha256':hashlib.sha256(args.csv.read_bytes()).hexdigest(),'rows':len(unique),'origin':'simulated'},indent=2))
    with target.open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
    with connect('import_lab_charge_reference') as conn:
        with conn.cursor() as cur:cur.executemany('INSERT INTO hist.lab_charge_reference VALUES(%s,%s,%s,%s) ON CONFLICT(tank_code,day) DO UPDATE SET amp_hours=EXCLUDED.amp_hours,source=EXCLUDED.source',[(r['tank_code'],r['day'],r['amp_hours'],r['source']) for r in rows])
    print(f'Loaded {len(rows)} daily reference totals')
if __name__=='__main__':main()
