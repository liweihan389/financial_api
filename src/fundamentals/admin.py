"""Operational SQLite is independent of immutable financial/market snapshots."""
from contextlib import contextmanager
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from fastapi import HTTPException
from .market_store import import_market,source_csv,digest_file
from .importer import import_file

class Admin:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'operations.sqlite3';self.incoming=self.root/'incoming';self.incoming.mkdir(exist_ok=True)
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='data-import');self.lock=threading.Lock()
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,created TEXT,channel TEXT,name TEXT,parameters TEXT,status INTEGER,seconds REAL,generation TEXT);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,name TEXT,size INTEGER,offset INTEGER,status TEXT,sha256 TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,upload_id TEXT,kind TEXT,status TEXT,progress TEXT,error TEXT,created TEXT,result TEXT);''')
            c.execute("UPDATE jobs SET status='interrupted',error='Service restarted; retry the import. Immutable active versions are preserved.' WHERE status IN ('queued','running')")
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()
    def log(self,channel,name,parameters,status,seconds,generation=None):
        def redact(v):
            if isinstance(v,dict):return {k:('[redacted]' if any(t in k.lower() for t in ['token','password','authorization','secret','user_agent','contact']) else redact(x)) for k,x in v.items()}
            if isinstance(v,list):return [redact(x) for x in v]
            return v
        safe=json.dumps(redact(parameters),default=str,ensure_ascii=False)[:12000]
        with self.connect() as c:
            c.execute('INSERT INTO requests(created,channel,name,parameters,status,seconds,generation) VALUES(?,?,?,?,?,?,?)',(dt.datetime.now(dt.timezone.utc).isoformat(),channel,name,safe,status,seconds,generation))
            c.execute('DELETE FROM requests WHERE id < (SELECT coalesce(max(id),0)-10000 FROM requests)')
    def history(self,limit=100,offset=0,query=''):
        if not 1<=limit<=1000 or offset<0:raise ValueError('invalid_paging')
        with self.connect() as c:
            total=c.execute('SELECT count(*) FROM requests WHERE name LIKE ?',['%'+query+'%']).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM requests WHERE name LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?',['%'+query+'%',limit,offset])]
        for r in rows:r['parameters']=json.loads(r['parameters'])
        return {'total':total,'rows':rows,'retention':'latest_10000_requests'}
    def create_upload(self,name,size):
        if not isinstance(name,str) or Path(name).name!=name or '/' in name or '\\' in name or not name.lower().endswith(('.zip','.csv','.json')):raise ValueError('invalid_upload_filename')
        if not isinstance(size,int) or not 0<size<=40*1024**3:raise ValueError('invalid_upload_size')
        import shutil
        if size+1024**3>shutil.disk_usage(self.incoming).free:raise ValueError('insufficient_upload_disk_space')
        uid=uuid.uuid4().hex
        with self.connect() as c:c.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?)',(uid,name,size,0,'uploading',None))
        (self.incoming/f'{uid}.part').touch()
        return self.upload(uid)
    def upload(self,uid):
        with self.connect() as c:row=c.execute('SELECT * FROM uploads WHERE id=?',[uid]).fetchone()
        if row is None:raise ValueError('unknown_upload')
        return dict(row)
    async def append(self,uid,offset,request):
        # Holding a dedicated lock per small chunk prevents overlapping writes.
        # SQLite conditional update also enforces the expected offset.
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>8*1024*1024:raise HTTPException(413,'chunk_limit_8MiB')
        with self.lock:
            row=self.upload(uid)
            if row['status']!='uploading' or offset!=row['offset'] or offset+len(data)>row['size']:raise ValueError('upload_offset_or_size_mismatch')
            p=self.incoming/f'{uid}.part'
            with p.open('r+b') as f:f.truncate(offset);f.seek(offset);f.write(data)
            with self.connect() as c:c.execute('UPDATE uploads SET offset=? WHERE id=? AND offset=?',[offset+len(data),uid,offset])
        return self.upload(uid)
    def finish(self,uid,expected_sha256=None):
        with self.lock:
            row=self.upload(uid)
            if row['offset']!=row['size']:raise ValueError('upload_incomplete')
            p=self.incoming/f'{uid}.part';final=self.incoming/(uid+Path(row['name']).suffix.lower())
            if row['status']=='ready':return self.upload(uid)
            actual=digest_file(p)
            if expected_sha256 and expected_sha256!=actual:raise ValueError('upload_checksum_mismatch')
            probe=self.incoming/(uid+'-probe'+Path(row['name']).suffix.lower())
            probe.unlink(missing_ok=True);os.link(p,probe)
            preview=None
            try:
                from .sec_store import probe as sec_probe
                if probe.suffix=='.json':preview=sec_probe(probe)
                elif probe.suffix=='.zip':
                    import zipfile
                    with zipfile.ZipFile(probe) as z:
                        if any(x.filename.lower().endswith('.json') and not x.filename.startswith('__MACOSX/') for x in z.infolist()):preview=sec_probe(probe)
                if preview is None:
                    with source_csv(probe) as (f,member):
                        header_bytes=f.readline(65537)
                        if len(header_bytes)>65536:raise ValueError('upload_header_limit_64KiB')
                        header=header_bytes.decode('utf-8-sig').strip()
                    if not (('PERMNO' in header and 'DlyCalDt' in header) or ('gvkey' in header and ('datadate' in header or 'permno' in header))):raise ValueError('unrecognized_dataset_header')
                    preview={'header':header.split(','),'member':member,'kind':'market' if 'PERMNO' in header else 'links' if 'permno' in header else 'financial'}
            finally:probe.unlink(missing_ok=True)
            if not final.exists():os.replace(p,final)
            with self.connect() as c:c.execute("UPDATE uploads SET status='ready',sha256=? WHERE id=?",[actual,uid])
        return {**self.upload(uid),'preview':preview}
    def _suffix_probe(self,p,name):
        final=self.incoming/(p.stem+Path(name).suffix.lower());os.replace(p,final);return final
    def start_job(self,uid,kind='market',scope='sample'):
        row=self.upload(uid)
        if row['status']!='ready':raise ValueError('upload_not_ready')
        if kind not in {'market','financial','links','sec'} or scope not in {'sample','full'}:raise ValueError('invalid_import_options')
        if kind!='sec' and scope=='full' and os.environ.get('FINANCIAL_ALLOW_FULL_IMPORT')!='1':raise ValueError('full_import_disabled_set_FINANCIAL_ALLOW_FULL_IMPORT')
        jid=uuid.uuid4().hex
        with self.connect() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(jid,uid,kind,'queued','{}',None,dt.datetime.now(dt.timezone.utc).isoformat(),None))
        def update(status,progress=None,error=None,result=None):
            with self.connect() as c:c.execute('UPDATE jobs SET status=?,progress=?,error=?,result=? WHERE id=?',(status,json.dumps(progress or {},default=str),error,json.dumps(result,default=str) if result else None,jid))
        def run():
            update('running')
            try:
                path=self.incoming/(uid+Path(row['name']).suffix.lower())
                if kind=='sec':
                    from .sec_store import SecStore
                    result=SecStore(self.root).import_file(path,progress=lambda x:update('running',x))
                elif kind=='market':result=import_market(path,self.root,scope,progress=lambda x:update('running',x))
                elif kind=='links':
                    from .market_links import import_links
                    result=import_links(path,self.root)
                else:result=import_file(path,self.root,progress=lambda x:update('running',{'message':str(x)}))
                update('complete',{'complete':True},result=result)
            except BaseException as exc:update('failed',error=str(exc))
        self.pool.submit(run);return {'job_id':jid,'status':'queued'}
    def jobs(self):
        with self.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]
        for r in rows:
            r['progress']=json.loads(r['progress']);r['result']=json.loads(r['result']) if r['result'] else None
        return {'rows':rows}
    def uploads(self):
        with self.connect() as c:return {'rows':[dict(r) for r in c.execute('SELECT * FROM uploads ORDER BY rowid DESC LIMIT 100')]}
