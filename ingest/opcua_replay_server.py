"""Replay real recordings through a read-only OPC UA endpoint."""
import argparse,asyncio,json,logging
from datetime import datetime,timezone
from asyncua import Server,ua
from recordings import SKAB,STEEL,FILES,load_skab,load_steel,recording_id

async def add_stream(server,idx,path,rows,columns,source,rid,max_hold=None):
    obj=server.nodes.objects
    for name in path:
        found=None
        for child in await obj.get_children():
            if (await child.read_browse_name()).Name==name:found=child;break
        obj=found or await obj.add_object(idx,name)
    variables={}
    for col,unit in columns.items():
        var=await obj.add_variable(idx,col.replace(' ',''),0.,varianttype=ua.VariantType.Double)
        metadata=json.dumps({'unit':unit,'origin':'real','source':source,'recording_id':rid,'max_hold':max_hold})
        await var.write_attribute(ua.AttributeIds.Description,ua.DataValue(ua.Variant(ua.LocalizedText(metadata))))
        await var.write_value(ua.DataValue(ua.Variant(0.,ua.VariantType.Double),StatusCode=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData)))
        variables[col]=var
    return rows,variables

async def prepare(endpoint):
    server=Server();await server.init();server.set_endpoint(endpoint)
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity]);server.set_server_name('Partec project 3 recording replay')
    idx=await server.register_namespace('urn:partec:historian:replay');streams=[]
    for relative in FILES:
        rows=load_skab(relative);rid=recording_id(relative)
        # Maximum hold age for change-only reconstruction: above the longest unchanged run in the 1 Hz rig
        # recordings (64 s) and their longest source gap (76 s). Labels are step signals without an age limit.
        streams.append(await add_stream(server,idx,['SKAB',rid,'WaterLoop'],rows,{k:v for k,v in SKAB.items() if k not in ('anomaly','changepoint')},'skab:'+relative,rid,'2 minutes'))
        streams.append(await add_stream(server,idx,['SKAB',rid,'Labels'],rows,{k:'flag' for k in ('anomaly','changepoint')},'skab:'+relative,rid))
    # Quarter-hour meter: no reading in the source repeats for longer than 1 h 45 min.
    streams.append(await add_stream(server,idx,['SteelPlant','MainMeter'],load_steel(),STEEL,'uci:851','steel2018','6 hours'))
    return server,streams

async def replay(stream,interval=.01,batch=1):
    rows,variables=stream
    for i,row in enumerate(rows):
        for col,var in variables.items():
            await var.write_value(ua.DataValue(ua.Variant(row[col],ua.VariantType.Double),SourceTimestamp=row['ts'],ServerTimestamp=datetime.now(timezone.utc)))
        # Pacing affects wall-clock runtime only; recorded gaps and timestamps stay intact.
        if i%batch==batch-1:await asyncio.sleep(interval)

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--endpoint',default='opc.tcp://127.0.0.1:4843/historian/');ap.add_argument('--delay',type=float,default=15);ap.add_argument('--interval',type=float,default=.01);args=ap.parse_args()
    if args.interval<=0:raise ValueError('interval must be positive')
    server,streams=await prepare(args.endpoint)
    async with server:
        await asyncio.sleep(args.delay)
        await asyncio.gather(*(replay(s,args.interval,20 if i==len(streams)-1 else 1) for i,s in enumerate(streams)))
        await asyncio.sleep(3)
if __name__=='__main__':logging.basicConfig(level=logging.WARNING);asyncio.run(main())
