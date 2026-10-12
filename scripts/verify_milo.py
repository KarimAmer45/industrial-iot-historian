"""Third-party interoperability check; read/subscription only, never writes server nodes."""
import asyncio,json,logging,pathlib,sys
from datetime import datetime,timezone
from asyncua import Client
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from opcua_collector import collect

async def main():
    endpoint='opc.tcp://milo.digitalpetri.com:62541/milo';report={'endpoint':endpoint,'checked_at':datetime.now(timezone.utc).isoformat()}
    try:
        async with Client(endpoint,timeout=10) as client:
            names=[(await n.read_browse_name()).Name for n in await client.nodes.objects.get_children()]
        report['objects']=names
        print(f'Milo top-level objects: {names}',flush=True)
        chosen=['Dynamic'] if 'Dynamic' in names else ['Demo/Dynamic'] if 'Demo' in names else []
        if not chosen:raise RuntimeError(f'No known demonstration objects; inspect browse result {names}')
        stop=asyncio.Event();ready=asyncio.Event()
        async def timer():await asyncio.wait_for(ready.wait(),45);await asyncio.sleep(12);stop.set()
        task=asyncio.create_task(timer())
        try:report['collector']=await asyncio.wait_for(collect(endpoint,chosen,stop,ready,origin='simulated',prefix='Milo.',retry_seconds=10),75)
        finally:task.cancel()
        report['verified']=report['collector'].get('received',0)>0
    except Exception as exc:
        report['verified']=False;report['error']=f'{type(exc).__name__}: {exc}'
    (ROOT/'docs/results/milo.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
    if not report['verified']:raise SystemExit(1)
if __name__=='__main__':logging.basicConfig(level=logging.WARNING);asyncio.run(main())
