"""Export actual database evidence and publication-style figures."""
import csv,json,pathlib,sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    out=ROOT/'docs/results';out.mkdir(parents=True,exist_ok=True)
    queries={'signal_counts':'SELECT * FROM hist.v_signal_counts ORDER BY tag',
      'scores':'SELECT * FROM hist.v_scores ORDER BY recording_id',
      'baseline':'SELECT * FROM hist.baseline',
      'carrier_energy':'SELECT * FROM hist.v_ah_ratio ORDER BY started,step_id',
      'daily_charge':'SELECT * FROM hist.v_lab_comparison ORDER BY day',
      'steel_daily':'SELECT * FROM hist.v_steel_daily ORDER BY source_date',
      'excursions':'SELECT * FROM hist.v_excursion ORDER BY tag,started'}
    results={}
    with connect('export_historian') as conn:
        for name,sql in queries.items():
            cur=conn.execute(sql);columns=[c.name for c in cur.description];rows=cur.fetchall()
            with (out/f'{name}.csv').open('w',newline='',encoding='utf-8') as f:
                w=csv.writer(f);w.writerow(columns);w.writerows(rows)
            results[name]=[dict(zip(columns,r)) for r in rows]
        current=conn.execute("SELECT ts,current_a,anomaly,predicted FROM hist.v_skab_scored WHERE recording_id='valve1_1' ORDER BY ts").fetchall()
        summary={k:results[k] for k in ('scores','baseline','daily_charge')}
        summary['faults']=[r for r in results['carrier_energy'] if not .97<=r['ratio']<=1.03]
        summary['sample_count']=conn.execute('SELECT count(*) FROM hist.sample').fetchone()[0]
        (out/'summary.json').write_text(json.dumps(summary,default=str,indent=2),encoding='utf-8')
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,ax=plt.subplots(figsize=(12,4),layout='constrained');dates=[r[0] for r in current]
    ax.plot(dates,[r[1] for r in current],lw=.8,color='#176b87',label='Pump motor current')
    low,high=results['baseline'][0]['low_limit'],results['baseline'][0]['high_limit']
    ax.axhline(low,color='#b94b3f',ls='--',label='Independent baseline ±3 SD');ax.axhline(high,color='#b94b3f',ls='--')
    ax.fill_between(dates,0,1,where=[r[2] for r in current],transform=ax.get_xaxis_transform(),color='#edb44a',alpha=.22,label='Recorded anomaly')
    ax.set(ylabel='Current (A)',xlabel='Original source clock · assigned UTC',title='SKAB valve1/1 · real pump signal and ground truth');ax.legend(loc='upper right',fontsize=8)
    fig.savefig(out/'skab_current.png',dpi=150);plt.close(fig)
    energy=results['carrier_energy'];fig,ax=plt.subplots(figsize=(11,4),layout='constrained')
    ax.scatter([r['started'] for r in energy],[r['ratio'] for r in energy],s=12,c=['#b94b3f' if r['fault_expected'] else '#176b87' for r in energy])
    ax.axhline(1,color='#777777',ls='--');ax.set(ylabel='Measured Ah / planned Ah',xlabel='Dwell start · UTC',title=f"Simulated rectifiers · {len(energy)} carrier steps · three planted fault exposures")
    fig.savefig(out/'carrier_charge.png',dpi=150);plt.close(fig)
    # Compression differs by dataset, so no single headline ratio: one bar per dataset, sizes as labels.
    ts=json.loads((out/'timescale.json').read_text());sets=ts['datasets'][::-1];fig,ax=plt.subplots(figsize=(8,3.2),layout='constrained')
    bars=ax.barh([d['dataset'] for d in sets],[d['compression_ratio'] for d in sets],color='#176b87',height=.55)
    ax.bar_label(bars,labels=[f"{d['compression_ratio']:.1f}:1  ({d['rowstore_bytes']/1e6:.1f} MB → {d['columnstore_bytes']/1e6:.1f} MB, {d['rows']:,} rows)" for d in sets],padding=5,fontsize=9)
    ax.set_xlim(0,max(d['compression_ratio'] for d in sets)*1.9);ax.set(xlabel='Fresh rowstore bytes / columnstore bytes · same rows',title=f"TimescaleDB {ts['timescale_version']} · columnstore compression by dataset")
    fig.savefig(out/'compression.png',dpi=150);plt.close(fig)
    print('Exported seven result tables, summary and three figures')
if __name__=='__main__':main()
