"""Session-wide, append-only decoded EEG and aligned event journal (SQLite/WAL).

Independent writer commits at least once a second. A bounded queue avoids doing
disk I/O in the SDK callback. Never fabricates samples across a disconnected run.
"""
import json
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import numpy as np


class ContinuousStorageError(RuntimeError):
    pass


POLICY = dict(version='session-continuous-v1', file='continuous_eeg.sqlite3',
    begins='first_start_command', ends='session_finished_or_explicit_stop',
    includes=['capture','context','rest','events'],
    labels='replay_markers_and_intervals_on_same_perf_counter_clock',
    commit_interval_seconds=1, hardware_sync_verified=False)


def encode(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False)


class ContinuousEEG:
    def __init__(self, folder, metadata, capacity=60000):
        self.path=Path(folder)/POLICY['file']
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.queue=queue.Queue(maxsize=capacity)
        self.error=None;self.closed=False;self.closing=False;self.written=0;self.committed=0;self.accepted=0
        self.ready=threading.Event();self.last_sample=None;self.first_sample=None
        self.metadata=metadata;self.thread=threading.Thread(target=self._run,name='continuous-eeg-writer',daemon=True)
        self.thread.start()
        if not self.ready.wait(5):self.error='Continuous EEG database startup timed out'
        self.check()

    def check(self):
        if self.error:raise ContinuousStorageError('连续 EEG 保存失败：'+self.error)

    def send(self,kind,value):
        self.check()
        if self.closing or self.closed:raise ContinuousStorageError('连续 EEG 文件已关闭')
        try:self.queue.put_nowait((kind,value))
        except queue.Full:
            self.error='磁盘写入队列已满；未静默丢弃 EEG，请检查磁盘'
            self.check()

    def segment(self,source):
        ident=uuid.uuid4().hex
        self.send('segment',dict(id=ident,source=source,start=time.perf_counter(),unix=time.time()))
        self.event('connection_started',segment=ident,source=source)
        return ident

    def end_segment(self,ident,reason,diagnostics=None):
        self.send('segment_end',dict(id=ident,end=time.perf_counter(),reason=reason,diagnostics=diagnostics))
        self.event('connection_ended',segment=ident,reason=reason)

    def sample(self,segment,t,values,device,arrival):
        self.send('sample',(segment,float(t),list(values),device,float(arrival)))
        self.accepted+=1
        if self.first_sample is None:self.first_sample=float(t)
        self.last_sample=float(t)

    def event(self,kind,host_time=None,**data):
        self.send('event',dict(kind=kind,host_time=time.perf_counter() if host_time is None else host_time,data=data))

    def trial(self,attempt,record):
        # Latest status includes withdrawals; earlier status changes remain in events.
        self.send('trial',(attempt,record))

    def flush(self,timeout=15):
        barrier=threading.Event();self.send('flush',barrier)
        deadline=time.perf_counter()+timeout
        while not barrier.wait(.02):
            self.check()
            if time.perf_counter()>deadline:raise ContinuousStorageError('连续 EEG 写入确认超时')
        self.check()

    def close(self,reason):
        if self.closed:self.check();return
        if not self.closing:
            if not self.error:self.send('close',reason)
            self.closing=True
        self.thread.join(20)
        if self.thread.is_alive():self.error=self.error or '连续 EEG 写入线程未结束'
        self.closed=not self.thread.is_alive();self.check()

    def snapshot(self):
        return dict(enabled=True,path=str(self.path),active=not self.closed and not self.closing,
            samples_received=self.accepted,samples_committed=self.committed,first_sample=self.first_sample,
            last_sample=self.last_sample,error=self.error,policy=POLICY)

    def _run(self):
        con=None;pending=[];segment=None;last_commit=time.perf_counter();last_by_segment={};sources={}
        def write_samples():
            nonlocal pending
            if not pending:return
            t=np.asarray([r[1] for r in pending],dtype='<f8');x=np.asarray([r[2] for r in pending],dtype='<f8')
            arrivals=np.asarray([r[4] for r in pending],dtype='<f8')
            con.execute('INSERT INTO chunks(segment,row_start,n,channels,t_min,t_max,timestamps,eeg,arrivals,device) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (pending[0][0],self.written,len(t),x.shape[1],float(t.min()),float(t.max()),t.tobytes(),x.tobytes(),arrivals.tobytes(),encode([r[3] for r in pending])))
            self.written+=len(t);pending=[]
        try:
            con=sqlite3.connect(self.path,timeout=10)
            con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=FULL')
            con.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,start REAL,end REAL,source TEXT,reason TEXT,diagnostics TEXT,wall_unix REAL);
              CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,segment TEXT,row_start INTEGER,n INTEGER,channels INTEGER,t_min REAL,t_max REAL,timestamps BLOB,eeg BLOB,arrivals BLOB,device TEXT);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,host_time REAL,kind TEXT,data TEXT);
              CREATE TABLE IF NOT EXISTS trials(attempt INTEGER PRIMARY KEY,data TEXT);
              CREATE INDEX IF NOT EXISTS chunk_time ON chunks(t_min,t_max);
              CREATE INDEX IF NOT EXISTS event_time ON events(host_time);''')
            if 'wall_unix' not in {r[1] for r in con.execute('PRAGMA table_info(segments)')}:
                con.execute('ALTER TABLE segments ADD COLUMN wall_unix REAL')
            existing=con.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            identity={k:self.metadata[k] for k in ('subject','session_id')}
            if existing and json.loads(existing[0])!=identity:raise ValueError('Continuous archive session identity mismatch')
            self.written=con.execute('SELECT COALESCE(SUM(n),0) FROM chunks').fetchone()[0]
            self.committed=self.written
            self.accepted=self.written
            con.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('identity',encode(identity)))
            con.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('policy',encode(POLICY)))
            con.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('state',encode(dict(status='recording',opened_unix=time.time()))))
            # An unclosed segment indicates a prior process crash; never silently bridge it.
            con.execute("UPDATE segments SET reason='process_interrupted' WHERE end IS NULL AND reason IS NULL")
            con.commit();self.ready.set()
            while True:
                if self.error:raise ContinuousStorageError(self.error)
                try:kind,value=self.queue.get(timeout=max(.001,1-(time.perf_counter()-last_commit)))
                except queue.Empty:kind,value='tick',None
                if kind=='sample':
                    if segment!=value[0]:write_samples();segment=value[0]
                    rate=sources[segment]['sample_rate'];prev=last_by_segment.get(segment)
                    if prev is not None and (value[1]<=prev or value[1]-prev>1.8/rate):
                        con.execute('INSERT INTO events(host_time,kind,data) VALUES(?,?,?)',(value[1],'sample_discontinuity',encode(dict(segment=segment,previous=prev,current=value[1],gap=value[1]-prev))))
                    last_by_segment[segment]=value[1];pending.append(value)
                    if len(pending)>=rate:write_samples()
                elif kind=='segment':
                    sources[value['id']]=value['source']
                    con.execute('INSERT INTO segments(id,start,source,wall_unix) VALUES(?,?,?,?)',(value['id'],value['start'],encode(value['source']),value['unix']))
                elif kind=='segment_end':
                    con.execute('UPDATE segments SET end=?,reason=?,diagnostics=? WHERE id=?',(value['end'],value['reason'],encode(value['diagnostics']),value['id']))
                elif kind=='event':
                    con.execute('INSERT INTO events(host_time,kind,data) VALUES(?,?,?)',(value['host_time'],value['kind'],encode(value['data'])))
                elif kind=='trial':con.execute('INSERT OR REPLACE INTO trials VALUES(?,?)',(value[0],encode(value[1])))
                if kind in ('flush','close','tick') or time.perf_counter()-last_commit>=1:
                    write_samples();con.commit();self.committed=self.written;last_commit=time.perf_counter()
                if kind=='flush':value.set()
                if kind=='close':
                    con.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('state',encode(dict(status='closed',reason=value,closed_unix=time.time(),samples=self.written))))
                    con.commit();con.execute('PRAGMA wal_checkpoint(TRUNCATE)');break
        except Exception as exc:
            self.error=self.error or str(exc)
            # Any already committed chunk remains readable even after disk/queue failure.
        finally:
            if con:con.close()
            self.ready.set()


def iter_eeg(path,start=None,end=None):
    """Read committed batches without loading a whole multi-hour recording into RAM."""
    con=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        query='SELECT segment,row_start,n,channels,timestamps,eeg,arrivals,device FROM chunks'
        where=[];args=[]
        if start is not None:where.append('t_max>=?');args.append(start)
        if end is not None:where.append('t_min<?');args.append(end)
        if where:query+=' WHERE '+' AND '.join(where)
        for seg,row,n,c,t,x,a,device in con.execute(query+' ORDER BY id',args):
            ts=np.frombuffer(t,dtype='<f8');mask=np.ones(n,dtype=bool)
            if start is not None:mask&=ts>=start
            if end is not None:mask&=ts<end
            if mask.any():yield dict(segment=seg,row_index=np.arange(row,row+n)[mask],sample_monotonic=ts[mask],
                eeg=np.frombuffer(x,dtype='<f8').reshape(n,c)[mask],arrival_monotonic=np.frombuffer(a,dtype='<f8')[mask],
                device_timestamp=np.asarray(json.loads(device),dtype=object)[mask])
    finally:con.close()


def replay_labels(path):
    """Interval/event labels retain original kind and meaning; normal P is NOT relabeled as error."""
    from eeg import align_labels
    con=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        records=[json.loads(r[0]) for r in con.execute('SELECT data FROM trials ORDER BY attempt')]
        unfinished={r['attempt']:r for r in records if r['status']=='recording'}
        if unfinished:
            for (data,) in con.execute("SELECT data FROM events WHERE kind='replay_marker' ORDER BY id"):
                event=json.loads(data)
                if event['attempt'] in unfinished:unfinished[event['attempt']]['markers'].append(event['marker'])
            for record in unfinished.values():
                if record['markers']:
                    record.update(start=record['markers'][0]['monotonic'],end=record['markers'][-1]['monotonic'],
                        recovered_marker_range=True,note='Only committed marker range; unfinished trial is not complete')
    finally:con.close()
    result=[]
    for r in records:
        marks=r['markers'];ann=r.get('annotation') or {}
        if len(marks)<2:continue
        _,mapped=align_labels(np.empty(0),marks,ann.get('intervals',[]))
        src=np.array([m['source_seconds'] for m in marks]);host=np.array([m['monotonic'] for m in marks])
        for e in mapped:
            row=dict(e,attempt=r['attempt'],trial_index=r['index'],round=r['round'],dataset=r['dataset'],episode=r['episode'],
                trial_status=r['status'],annotation_id=ann.get('id'),segment=r.get('segment'))
            if e.get('kind')=='point' and src[0]<=e['press_time']<=src[-1]:
                row['point_monotonic']=float(np.interp(e['press_time'],src,host))
            result.append(row)
    return records,result


def export_npz(path,output,start=None,end=None):
    """Optional offline export; run AFTER recording. The live file stays bounded-memory SQLite."""
    batches=list(iter_eeg(path,start,end))
    if not batches:raise ValueError('No EEG samples in requested window')
    ts=np.concatenate([b['sample_monotonic'] for b in batches]);x=np.concatenate([b['eeg'] for b in batches])
    segments=np.concatenate([np.full(len(b['sample_monotonic']),b['segment']) for b in batches])
    records,labels=replay_labels(path);attempt=np.zeros(len(ts),dtype=np.int32)
    for r in records:
        if r['start'] is not None:attempt[(segments==r['segment'])&(ts>=r['start'])&(ts<r['end'])]=r['attempt']
    np.savez_compressed(output,eeg=x,sample_monotonic=ts,arrival_monotonic=np.concatenate([b['arrival_monotonic'] for b in batches]),
        segment=segments,
        row_index=np.concatenate([b['row_index'] for b in batches]),trial_attempt=attempt,
        replay_index_json=np.array(encode(records)),label_intervals_json=np.array(encode(labels)))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='Export session continuous EEG to NPZ; keeps replay and interval labels')
    p.add_argument('database',type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--start',type=float);p.add_argument('--end',type=float);args=p.parse_args()
    if args.output.exists():p.error('Output already exists; choose a new filename')
    export_npz(args.database,args.output,args.start,args.end)
