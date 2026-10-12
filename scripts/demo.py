"""Self-contained end-to-end run. External Milo and Project 1 writes are explicit extras."""
import pathlib,re,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'ingest'),str(ROOT/'scripts')]
from common import connect
from timescale import enable,measure

def run(path,*args):subprocess.run([sys.executable,str(ROOT/path),*args],cwd=ROOT,check=True)
def main():
    run('scripts/migrate.py');enable()
    with connect('demo_check') as conn:seeded=conn.execute('SELECT EXISTS(SELECT 1 FROM trace.lot)').fetchone()[0]
    if not seeded:run('ingest/p1_simulate_plant.py')
    run('scripts/download_data.py');run('scripts/replay_demo.py');run('ingest/rectifiers.py')
    run('scripts/import_lab_reference.py');measure();run('scripts/export_results.py');run('scripts/build_grafana.py')
    try:subprocess.run([sys.executable,'-m','pytest','-q','tests','--junitxml=docs/results/tests.xml'],cwd=ROOT,check=True)
    finally: # the JUnit file should not record the machine name
        xml=ROOT/'docs/results/tests.xml'
        if xml.exists():xml.write_text(re.sub(r' hostname="[^"]*"','',xml.read_text(encoding='utf-8')),encoding='utf-8')
if __name__=='__main__':main()
