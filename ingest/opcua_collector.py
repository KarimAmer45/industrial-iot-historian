"""Read-only OPC UA subscription with bounded queue, explicit quality, gap markers and final flush.

Every interval the collector cannot vouch for is written to hist.acquisition_gap: bad, non-numeric
or untimestamped values, values the server discarded from its queue (Overflow info bit), lost
connections and collector stops. A different value for an already recorded tag/timestamp is
quarantined and counted while the rest of the batch commits. Each outage gets its own bounded
retry interval. Reconnects recover the live stream, not missed history (no server history support).
"""
import argparse,asyncio,json,logging,math
from collections import Counter
from contextlib import suppress
from datetime import datetime,timezone
from asyncua import Client,ua
from common import connect,ROOT

RETRYABLE=(OSError,asyncio.TimeoutError,ConnectionError)

def overflowed(status):
    """InfoType=DataValue (0x0400) with the Overflow bit (0x0080): the server dropped queued values before this one."""
    return status.value&0x0C00==0x0400 and bool(status.value&0x0080)

class Handler:
    def __init__(self,queue,mapping,open_gaps=None,stopped=None):
        self.queue=queue;self.mapping=mapping;self.stats=Counter();self.failure=None
        self.last_seen={};self.open_gaps={sid:list(v) for sid,v in (open_gaps or {}).items()}
        self.stopped={(sid,f) for sid,v in (stopped or {}).items() for f in v};self.confirmed=Counter()
    def put(self,item):
        try:self.queue.put_nowait(item);return True
        except asyncio.QueueFull:self.failure=RuntimeError('Collector queue overflow: acquisition is incomplete');return False
    def open_gap(self,sid,from_ts,reason):
        if from_ts is None:return
        opened=self.open_gaps.setdefault(sid,[])
        if any(f<=from_ts for f in opened):return # an earlier open gap already covers it
        opened.append(from_ts);self.stats['gaps_opened']+=1;self.put(('open',sid,from_ts,reason))
    def observed(self,sid,ts):
        # The first value after a gap closes it; an unchanged value with the same timestamp means nothing was lost.
        # First notification of a subscription: the server's current value with the source timestamp of its
        # last change. If that change precedes a 'collector stopped/failed' gap, the value did not change inside it.
        initial=self.confirmed.get(sid)==1
        for f in [f for f in self.open_gaps.get(sid,[]) if f<=ts or (initial and (sid,f) in self.stopped)]:
            self.open_gaps[sid].remove(f);self.put(('close',sid,f,ts) if ts>f else ('drop',sid,f))
        self.last_seen[sid]=max(ts,self.last_seen.get(sid,ts))
    def datachange_notification(self,node,val,data):
        dv=data.monitored_item.Value;sid=self.mapping[node.nodeid];status=dv.StatusCode or ua.StatusCode();self.confirmed[sid]+=1
        ts=dv.SourceTimestamp
        if ts is not None and ts.tzinfo is None:ts=ts.replace(tzinfo=timezone.utc) # UA DateTime is UTC
        if status.is_bad():
            self.stats['bad_skipped']+=1
            # "No value yet" loses nothing; any other bad status makes the signal unknown from then on.
            if status.value&0xFFFF0000!=ua.StatusCodes.BadWaitingForInitialData:self.open_gap(sid,ts or self.last_seen.get(sid),'bad status')
            return
        if ts is None:self.stats['missing_source_timestamp']+=1;self.open_gap(sid,self.last_seen.get(sid),'value without source timestamp');return
        try:
            if isinstance(val,bool):raise ValueError('boolean')
            value=float(val)
            if not math.isfinite(value):raise ValueError('nonfinite')
        except (ValueError,TypeError):self.stats['nonnumeric_skipped']+=1;self.open_gap(sid,ts,'non-numeric or non-finite value');return
        if overflowed(status):
            self.stats['server_queue_overflow']+=1;self.put(('gap',sid,self.last_seen.get(sid),ts,'server queue overflow'))
        if self.put(('sample',sid,ts,value,0 if status.is_good() else 1)):self.stats['received']+=1;self.observed(sid,ts)
    def status_change_notification(self,status):self.stats['status_changes']+=1

async def walk(node,path,seen=None,depth=0):
    seen=set() if seen is None else seen
    if node.nodeid in seen or depth>12:return
    seen.add(node.nodeid)
    for child in await node.get_children():
        name=(await child.read_browse_name()).Name;kind=await child.read_node_class()
        if kind==ua.NodeClass.Variable:yield '.'.join(path+[name]),child
        elif kind==ua.NodeClass.Object:
            async for entry in walk(child,path+[name],seen,depth+1):yield entry

async def register(conn,tag,node,origin,source):
    meta={'origin':origin,'source':source,'unit':None,'recording_id':None,'max_hold':None}
    desc=(await node.read_description()).Text or ''
    try:
        parsed=json.loads(desc)
        if isinstance(parsed,dict):meta.update({k:parsed[k] for k in meta if k in parsed})
    except ValueError:pass
    if meta['origin'] not in ('real','simulated'):raise ValueError('Unknown signal origin')
    existing=conn.execute('SELECT signal_id,origin,source,unit,recording_id FROM hist.signal WHERE tag=%s',(tag,)).fetchone()
    identity=(meta['origin'],meta['source'],meta['unit'],meta['recording_id'])
    if existing:
        if tuple(existing[1:])!=identity:raise ValueError(f'Signal provenance changed for {tag}')
        # Acquisition settings are configuration, not provenance: a subscription stores changes only.
        conn.execute("UPDATE hist.signal SET acquisition_mode='change_only',max_hold=%s::interval WHERE signal_id=%s AND (acquisition_mode,max_hold) IS DISTINCT FROM ('change_only',%s::interval)",(meta['max_hold'],existing[0],meta['max_hold']))
        return existing[0]
    return conn.execute("INSERT INTO hist.signal(tag,description,origin,source,unit,recording_id,acquisition_mode,max_hold) VALUES(%s,%s,%s,%s,%s,%s,'change_only',%s::interval) RETURNING signal_id",(tag,desc,*identity,meta['max_hold'])).fetchone()[0]

GAP_SQL={
 'open':('INSERT INTO hist.acquisition_gap(signal_id,from_ts,reason) SELECT %(sid)s,%(a)s,%(reason)s WHERE NOT EXISTS('
  'SELECT 1 FROM hist.acquisition_gap WHERE signal_id=%(sid)s AND to_ts IS NULL AND from_ts<=%(a)s)'),
 'close':'UPDATE hist.acquisition_gap SET to_ts=%(b)s WHERE signal_id=%(sid)s AND from_ts=%(a)s AND to_ts IS NULL',
 'drop':'DELETE FROM hist.acquisition_gap WHERE signal_id=%(sid)s AND from_ts=%(a)s AND to_ts IS NULL',
 # Overflow: unknown since the last value received (or stored) before the flagged one.
 'gap':('INSERT INTO hist.acquisition_gap(signal_id,from_ts,to_ts,reason) SELECT %(sid)s,f,%(b)s,%(reason)s FROM (SELECT coalesce('
  '%(a)s::timestamptz,(SELECT max(ts) FROM hist.sample WHERE signal_id=%(sid)s AND ts<%(b)s)) AS f) x WHERE f<%(b)s')}

def flush(conn,batch,gaps=()):
    """Commit samples and gap markers together. A value that differs from one already recorded for the same
    signal/timestamp (in the database or earlier in this batch) is quarantined; the first value stands."""
    result=Counter()
    if not batch and not gaps:return result
    seen={};quarantine=[]
    for sid,ts,value,quality in batch:
        key=(sid,ts)
        if key in seen and seen[key]!=(value,quality):quarantine.append((sid,ts,value,quality,'different value in the same batch',sid,ts))
        else:seen.setdefault(key,(value,quality))
    with conn.transaction():
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS collector_batch (signal_id int,ts timestamptz,value float8,quality smallint) ON COMMIT DELETE ROWS')
        with conn.cursor().copy('COPY collector_batch FROM STDIN') as copy:
            for (sid,ts),(v,q) in seen.items():copy.write_row((sid,ts,v,q))
        stored=conn.execute('''INSERT INTO hist.sample_quarantine(signal_id,ts,value,quality,kept_value,kept_quality,reason)
          SELECT b.signal_id,b.ts,b.value,b.quality,s.value,s.quality,'different value already recorded'
          FROM collector_batch b JOIN hist.sample s USING(signal_id,ts) WHERE (s.value,s.quality) IS DISTINCT FROM (b.value,b.quality)''').rowcount
        result['inserted']+=conn.execute('INSERT INTO hist.sample SELECT * FROM collector_batch ON CONFLICT DO NOTHING').rowcount
        if quarantine: # kept value = whatever is recorded now (an older stored value or the batch's first one)
            with conn.cursor() as cur:cur.executemany('INSERT INTO hist.sample_quarantine(signal_id,ts,value,quality,kept_value,kept_quality,reason) SELECT %s,%s,%s,%s,s.value,s.quality,%s FROM hist.sample s WHERE s.signal_id=%s AND s.ts=%s',quarantine)
        if stored+len(quarantine):result['quarantined']+=stored+len(quarantine)
        for op in gaps:
            kind,sid,*rest=op;args={'sid':sid,'a':rest[0],'b':None,'reason':None}
            if kind=='open':args['reason']=rest[1]
            elif kind=='close':args['b']=rest[1]
            elif kind=='gap':args.update(b=rest[1],reason=rest[2])
            if conn.execute(GAP_SQL[kind],args).rowcount and kind in ('open','gap'):result['gaps_recorded']+=1
    return result

async def drain(conn,queue,stats,limit=None):
    batch=[];gaps=[]
    while not queue.empty() and (limit is None or len(batch)+len(gaps)<limit):
        item=queue.get_nowait()
        if item[0]=='sample':batch.append(item[1:])
        else:gaps.append(item)
    stats.update(await asyncio.to_thread(flush,conn,batch,gaps))

async def mark_unvouched(conn,queue,stats,handler,signals,reason):
    """Mark from where the database can no longer vouch for each signal. After a normal stop that is the stop
    itself; after a failure, the last value received (changes since then may be lost)."""
    await drain(conn,queue,stats) # after a queue overflow the queue is still full and would refuse the markers
    now=datetime.now(timezone.utc)
    for sid in signals:
        last=handler.last_seen.get(sid)
        handler.open_gap(sid,max(last or now,now) if reason=='collector stopped' else last,reason)
    await drain(conn,queue,stats)

async def collect(endpoint,objects,stop,ready=None,origin='real',prefix='',retry_seconds=20):
    stats=Counter();queue=asyncio.Queue(maxsize=200000);loop=asyncio.get_running_loop();deadline=None
    while not stop.is_set():
        conn=None;handler=None
        try:
            async with Client(endpoint,timeout=10) as client:
                conn=connect('opcua_collector');conn.commit();conn.autocommit=True
                mapping={};nodes=[]
                for requested in objects:
                    obj=client.nodes.objects;parts=requested.split('/')
                    for part in parts:
                        matches=[child for child in await obj.get_children() if (await child.read_browse_name()).Name==part]
                        if len(matches)!=1:raise ValueError(f'Ambiguous or missing object path: {requested}')
                        obj=matches[0]
                    async for tag,node in walk(obj,parts):
                        if len(nodes)>=4096:raise ValueError('Browse scope exceeds 4096 nodes; select a narrower object path')
                        mapping[node.nodeid]=await register(conn,prefix+tag,node,origin,endpoint);nodes.append(node)
                if not nodes:raise ValueError(f'No variables found below {objects}')
                signals=sorted(set(mapping.values()));open_gaps={};stopped={}
                for sid,f,why in conn.execute('SELECT signal_id,from_ts,reason FROM hist.acquisition_gap WHERE to_ts IS NULL AND signal_id=ANY(%s)',(signals,)):
                    open_gaps.setdefault(sid,[]).append(f)
                    if why in ('collector stopped','collector failed'):stopped.setdefault(sid,[]).append(f)
                handler=Handler(queue,mapping,open_gaps,stopped)
                sub=await client.create_subscription(50,handler)
                handles=await sub.subscribe_data_change(nodes,queuesize=10000,sampling_interval=0)
                if any(isinstance(h,ua.StatusCode) and h.is_bad() for h in handles):raise RuntimeError('Server rejected monitored items')
                deadline=None # the retry interval is bounded per outage, counted from its first failure
                if ready:ready.set()
                reason='collector failed'
                try:
                    while not stop.is_set():
                        await asyncio.sleep(.1)
                        if handler.failure:raise handler.failure
                        await drain(conn,queue,stats,20000)
                        await client.check_connection()
                    reason='collector stopped'
                except RETRYABLE:
                    reason='connection lost';raise
                finally:
                    with suppress(Exception):await asyncio.wait_for(sub.delete(),5)
                    await mark_unvouched(conn,queue,stats,handler,signals,reason)
                    stats.update(handler.stats)
                return dict(stats)
        except RETRYABLE as exc:
            stats['connection_retries']+=1;now=loop.time()
            if deadline is None:deadline=now+retry_seconds
            if now>deadline:raise RuntimeError(f'OPC UA unavailable for {retry_seconds} s: {endpoint}') from exc
            await asyncio.sleep(1)
        finally:
            if conn:conn.close()
    return dict(stats)

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--endpoint',default='opc.tcp://127.0.0.1:4843/historian/');ap.add_argument('--objects',nargs='+',default=['SKAB','SteelPlant']);ap.add_argument('--origin',choices=['real','simulated'],default='real');ap.add_argument('--prefix',default='');ap.add_argument('--seconds',type=float,default=60);args=ap.parse_args()
    stop=asyncio.Event()
    async def timer():await asyncio.sleep(args.seconds);stop.set()
    timer_task=asyncio.create_task(timer())
    try:report=await collect(args.endpoint,args.objects,stop,origin=args.origin,prefix=args.prefix)
    finally:timer_task.cancel()
    print(json.dumps(report,indent=2))
if __name__=='__main__':logging.basicConfig(level=logging.WARNING);asyncio.run(main())
