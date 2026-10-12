"""Reviewed natural-key bridge to Project 1; dry run unless --apply is supplied."""
import argparse,json,pathlib,sys,uuid
from decimal import Decimal
import psycopg
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--target-env',required=True,type=pathlib.Path);ap.add_argument('--apply',action='store_true');ap.add_argument('--allow-unaudited',action='store_true',help='apply even if the target does not audit trace.load_step');args=ap.parse_args()
    target_url=next(line.split('=',1)[1] for line in args.target_env.read_text().splitlines() if line.startswith('DATABASE_URL='))
    with connect('export_carrier_energy') as src:
        rows=src.execute('''SELECT v.step_id,v.station,v.carrier_no,v.started,v.ended,v.measured_ah,
          sum(ll.qty_pcs*p.area_dm2) FROM hist.v_ah_ratio v JOIN trace.load_step st USING(step_id)
          JOIN trace.load_lot ll USING(load_id) JOIN trace.lot l USING(lot_id) JOIN ref.part p USING(part_id)
          WHERE coverage=1 GROUP BY v.step_id,v.station,v.carrier_no,v.started,v.ended,v.measured_ah ORDER BY v.step_id''').fetchall()
    updates=[];run_id=uuid.uuid4().hex[:12]
    with psycopg.connect(target_url,connect_timeout=10) as target:
        # Transaction-local label: Project 1's audit trigger on trace.load_step records it (changed_by)
        # together with the login, so every bridged amp_hours value can be traced back to this run.
        target.execute("SET TIME ZONE 'UTC'");target.execute("SELECT set_config('app.user',%s,true)",(f'project3_energy_bridge run {run_id}',))
        target.execute('SELECT pg_advisory_xact_lock(730001)')
        audited=target.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='trace.load_step'::regclass AND NOT tgisinternal AND tgfoid='audit.log_change()'::regprocedure AND tgtype&16=16)").fetchone()[0]
        if args.apply and not audited and not args.allow_unaudited:raise SystemExit('Target has no audit trigger on trace.load_step (update Project 1 first); use --allow-unaudited to apply anyway')
        for sid,station,carrier,a,b,ah,area in rows:
            found=target.execute('''SELECT st.step_id,st.amp_hours,sum(ll.qty_pcs*p.area_dm2)
              FROM trace.load_step st JOIN ref.station s USING(station_id) JOIN trace.load l USING(load_id)
              JOIN trace.load_lot ll USING(load_id) JOIN trace.lot lot USING(lot_id) JOIN ref.part p ON p.part_id=lot.part_id
              WHERE s.code=%s AND l.carrier_no=%s AND lower(st.in_station)=%s AND upper(st.in_station)=%s
              GROUP BY st.step_id''',(station,carrier,a,b)).fetchall()
            if len(found)!=1 or found[0][2]!=area:raise ValueError(f'Foundation mismatch at source step {sid}; no changes committed')
            tid,old,_=found[0];new=Decimal(str(ah)).quantize(Decimal('.001'))
            if old is not None and old!=new:raise ValueError(f'Existing Project 1 charge conflicts at {tid}; no overwrite')
            updates.append({'source_step':sid,'target_step':tid,'station':station,'carrier':carrier,'started':str(a),'ended':str(b),'old':str(old) if old is not None else None,'new':str(new)})
        pending=[r for r in updates if r['old']!=r['new']]
        if args.apply and pending:
            backup=ROOT/'backups';backup.mkdir(exist_ok=True)
            (backup/f'project1_energy_before_{run_id}.json').write_text(json.dumps(updates,indent=2))
            with target.cursor() as cur:
                cur.executemany('UPDATE trace.load_step SET amp_hours=%s WHERE step_id=%s AND amp_hours IS NOT DISTINCT FROM %s::numeric',[(r['new'],r['target_step'],r['old']) for r in pending])
                if cur.rowcount!=len(pending):raise ValueError('Concurrent target edit; no changes committed')
        report={'run_id':run_id,'target_audits_load_step':audited,'audit_label':f'project3_energy_bridge run {run_id}','applied':args.apply,'matched_steps':len(updates),'changed_steps':len(pending) if args.apply else 0,'pending_steps':len(pending),'matching':'station code, carrier, exact dwell bounds and load surface area'}
    filename='project1_bridge.json' if args.apply else 'project1_bridge_dry_run.json'
    (ROOT/'docs/results'/filename).write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if __name__=='__main__':main()
