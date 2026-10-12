"""Build in an isolated database and verify replay idempotency, then remove only it."""
import json,os,pathlib,subprocess,sys,time,uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
    prefix=sys.argv[1] if len(sys.argv)>1 else 'hist_verify_' # optional database name prefix
    name=prefix+uuid.uuid4().hex[:12];params=conninfo_to_dict(os.environ.get('DATABASE_URL',''));params['dbname']=name
    start=time.monotonic();report={'database':name,'runs':[]}
    with psycopg.connect(os.environ.get('DATABASE_URL',''),autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            env={**os.environ,'DATABASE_URL':make_conninfo(**params),'PGDATABASE':name}
            for script in ['scripts/demo.py','scripts/replay_demo.py']:
                r=subprocess.run([sys.executable,script],cwd=ROOT,env=env,capture_output=True,text=True)
                log=ROOT/'docs/results'/('clean_'+pathlib.Path(script).stem+'.log');log.write_text(r.stdout+'\n'+r.stderr,encoding='utf-8')
                report['runs'].append({'script':script,'returncode':r.returncode,'log':log.name})
                if r.returncode:raise RuntimeError(f'{script} failed: inspect {log}')
                with psycopg.connect(**params) as c:
                    total=c.execute('SELECT count(*) FROM hist.sample').fetchone()[0]
                    if 'sample_count' in report:assert total==report['sample_count']
                    report['sample_count']=total
                print(f'Clean verification passed: {script}; {total} samples',flush=True)
            report['replay_stable']=True
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            report['temporary_database_removed']=True;report['seconds']=round(time.monotonic()-start,2)
            (ROOT/'docs/results/clean_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
if __name__=='__main__':main()
