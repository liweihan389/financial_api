"""Immutable CRSP fragments and atomic manifests, separate from financial files."""
from contextlib import contextmanager
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import re
import time
import uuid
import zipfile
import duckdb
from .store import ident, literal, default_root

NUMERIC_FIELDS={'DlyPrc','DlyCap','DlyPrevPrc','DlyPrevCap','DlyRet','DlyRetx','DlyRetI','DlyOrdDivAmt','DlyNonOrdDivAmt','DlyFacPrc','DlyVol','DlyClose','DlyLow','DlyHigh','DlyBid','DlyAsk','DlyOpen','DlyNumTrd','DlyMMCnt','DlyPrcVol','ShrOut','DisDivAmt','DisFacPr','DisFacShr','vwretd','vwretx','ewretd','ewretx','sprtrn'}
POLICY = 'crsp-daily-v1'
WINDOWS = {'10000':('1986-01-01','1987-06-30'),'10001':('1998-01-01','2008-02-29'),
 '10026':('2004-01-01','2006-01-31'),'10042':('1998-01-01','2000-04-30'),
 '17778':('2008-01-01','2010-03-31'),'83443':('2008-01-01','2010-03-31'),
 '90319':('2004-08-19','2015-10-31'),'14542':('2014-04-03','2015-10-31'),
 '88490':('2023-01-01','2025-12-31'),'81046':('1999-01-01','2015-07-31'),
 '75039':('1999-01-01','2025-12-31'),'10025':('2015-01-01','2017-01-31'),
 '10065':('1998-01-01','2000-03-31')}
SNAPSHOTS = {'1925-12-31','1999-12-31','2025-12-31'}

@contextmanager
def source_csv(path):
    path=Path(path)
    if path.suffix.lower()=='.zip':
        with zipfile.ZipFile(path) as z:
            names=[n for n in z.namelist() if n.lower().endswith('.csv') and not n.startswith('__MACOSX/') and not Path(n).name.startswith('._')]
            if len(names)!=1: raise ValueError('zip_requires_one_main_csv')
            with z.open(names[0]) as stream: yield stream,names[0]
    elif path.suffix.lower()=='.csv':
        with path.open('rb') as stream: yield stream,path.name
    else: raise ValueError('expected_csv_or_zip')

def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def atomic_json(path,value):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    os.replace(temp,path)

class MarketStore:
    def __init__(self,root=None): self.root=Path(root or default_root()).resolve()/'market'
    def manifest(self,generation=None):
        if generation is None:
            p=self.root/'active.json'
            if not p.exists():return None
            generation=json.loads(p.read_text())['generation']
        if not isinstance(generation,str) or len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):raise ValueError('unknown_market_generation')
        p=self.root/'generations'/f'{generation}.json'
        if not p.is_file():raise ValueError('unknown_market_generation')
        from .telemetry import track_version
        track_version('market_generation',generation)
        return json.loads(p.read_text())
    @contextmanager
    def connect(self,generation=None):
        manifest=self.manifest(generation)
        if not manifest:raise ValueError('market_not_initialized')
        files=[str(self.root/p) for b in manifest['batches'] for p in b['files']]
        con=duckdb.connect()
        try:
            con.execute("SET memory_limit='1GB'");con.execute('SET threads=2')
            con.execute('CREATE VIEW market_raw AS SELECT * FROM read_parquet(['+','.join(literal(p) for p in files)+'],union_by_name=true)')
            con.execute('''CREATE VIEW market_current AS SELECT * EXCLUDE (_last_seq) FROM
              (SELECT *,max(try_cast(_sequence AS BIGINT)) OVER(PARTITION BY PERMNO,DlyCalDt) _last_seq FROM market_raw)
              WHERE try_cast(_sequence AS BIGINT)=_last_seq''')
            fields={name for batch in manifest['batches'] for name in batch['fields']}
            replacements=[f'CASE WHEN isfinite(try_cast({ident(name)} AS DOUBLE)) THEN try_cast({ident(name)} AS DOUBLE) END AS {ident(name)}' for name in sorted(fields & NUMERIC_FIELDS)]
            replacements += ['try_cast(_source_row AS BIGINT) AS _source_row','try_cast(_sequence AS BIGINT) AS _sequence']
            bad=[f'CASE WHEN {ident(name)} IS NOT NULL AND (try_cast({ident(name)} AS DOUBLE) IS NULL OR NOT isfinite(try_cast({ident(name)} AS DOUBLE))) THEN {literal(name)} END' for name in sorted(fields & NUMERIC_FIELDS)]
            invalid='list_filter(['+','.join(bad)+'],x -> x IS NOT NULL)' if bad else '[]::VARCHAR[]'
            con.execute('CREATE VIEW market_typed AS SELECT * REPLACE ('+','.join(replacements)+'),'+invalid+' AS _invalid_numeric_fields FROM market_current')
            dis_fields=','.join(ident(name) for name in sorted(fields) if name.startswith('Dis'))
            con.execute('''CREATE VIEW daily AS SELECT * EXCLUDE ('''+dis_fields+'''),count(DISTINCT _quote_hash) OVER(PARTITION BY PERMNO,DlyCalDt)>1 AS _quote_conflict
                FROM market_typed QUALIFY row_number() OVER(PARTITION BY PERMNO,DlyCalDt ORDER BY _source_row)=1''')
            con.execute("CREATE VIEW events AS SELECT DISTINCT * FROM market_typed WHERE nullif(DisSeqNbr,'') IS NOT NULL")
            yield con,manifest
        finally:con.close()


def import_market(source,root=None,scope='sample',progress=lambda _:None,memory='2GB'):
    if scope not in {'sample','full'}:raise ValueError('invalid_import_scope')
    store=MarketStore(root);store.root.mkdir(parents=True,exist_ok=True)
    lock=store.root/'import.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError('market_import_already_running')
    os.close(fd)
    folder=None
    started=time.monotonic()
    try:
        source_path=Path(source)
        expanded_size=source_path.stat().st_size
        if source_path.suffix.lower()=='.zip':
            with zipfile.ZipFile(source_path) as archive:
                members=[n for n in archive.namelist() if n.lower().endswith('.csv') and not n.startswith('__MACOSX/') and not Path(n).name.startswith('._')]
                if len(members)!=1:raise ValueError('zip_requires_one_main_csv')
                expanded_size=archive.getinfo(members[0]).file_size
        bounded_source=scope=='sample' and expanded_size<=256*1024**2
        fingerprint=digest_file(source);old=store.manifest()
        profile_input=[scope,WINDOWS,SNAPSHOTS and sorted(SNAPSHOTS),POLICY]
        if bounded_source:profile_input.append('bounded_uploaded_source')
        profile=hashlib.sha256(json.dumps(profile_input,sort_keys=True).encode()).hexdigest()
        if old and any(b['sha256']==fingerprint and b['profile']==profile for b in old['batches']):
            return {**old,'already_imported':True}
        sequence=max((b['sequence'] for b in old['batches']),default=0)+1 if old else 1
        batch=uuid.uuid4().hex;folder=store.root/'fragments'/batch;folder.mkdir(parents=True)
        raw_csv=folder/'staging.csv';files=[];rows=[];count=0;scanned=0;exceptions=[];selected_etfs=set();headers=None;source_member=None;buffer_bytes=0;sample_bytes=0
        con=duckdb.connect();con.execute('SET memory_limit='+literal(memory));con.execute('SET threads=2')
        def flush():
            nonlocal buffer_bytes
            if not rows:return
            with raw_csv.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(headers+['_batch_id','_source_sha256','_source_member','_source_row','_sequence','_row_hash','_quote_hash']);w.writerows(rows)
            dest=folder/f'{len(files):05d}.parquet'
            con.execute("COPY (SELECT * FROM read_csv("+literal(raw_csv)+",header=true,all_varchar=true,nullstr='',strict_mode=true)) TO "+literal(dest)+" (FORMAT PARQUET,COMPRESSION ZSTD)")
            files.append(str(dest.relative_to(store.root)));rows.clear();buffer_bytes=0;raw_csv.unlink()
        def scan(extra_etfs=False):
            nonlocal headers,source_member,count,scanned,buffer_bytes,sample_bytes
            with source_csv(source) as (stream,member):
                source_member=member
                header_line=stream.readline(65537)
                if len(header_line)>65536:raise ValueError('market_header_too_large')
                header=next(csv.reader([header_line.decode('utf-8-sig')]))
                if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',h) for h in header) or len(header)!=len(set(header)) or not {'PERMNO','DlyCalDt','DlyPrc','DlyRet','CUSIP','DisSeqNbr'}.issubset(header):raise ValueError('invalid_crsp_header')
                if headers is not None and header!=headers:raise ValueError('inconsistent_crsp_fields')
                headers=header;ix={h:i for i,h in enumerate(headers)}
                # Fast prefilter for these known CRSP exports. Parsing still uses
                # CSV for selected rows; malformed-width lines take the CSV path.
                for source_row,line in enumerate(iter(lambda:stream.readline(16*1024**2+1),b''),2):
                    if len(line)>16*1024**2:raise ValueError('market_row_limit_16MiB')
                    tokens=line.rstrip(b'\r\n').split(b',')
                    if len(tokens)==len(headers):
                        permno=tokens[ix['PERMNO']].decode();day=tokens[ix['DlyCalDt']].decode()
                    else:
                        try:
                            tokens=next(csv.reader([line.decode('utf-8')]))
                            if len(tokens)!=len(headers):raise ValueError('invalid_column_count')
                            permno=tokens[ix['PERMNO']];day=tokens[ix['DlyCalDt']]
                        except (ValueError,UnicodeError,csv.Error):
                            exceptions.append({'source_row':source_row,'reason':'invalid_csv_row'})
                            if len(exceptions)>1000:raise ValueError('too_many_invalid_rows')
                            continue
                    if not extra_etfs:scanned+=1
                    target=WINDOWS.get(permno)
                    hit=scope=='full' or bounded_source or day in SNAPSHOTS or (target is not None and target[0]<=day<=target[1])
                    if extra_etfs:hit=permno in selected_etfs and '2023-01-01'<=day<'2025-12-31' and not (target and target[0]<=day<=target[1])
                    if not hit:continue
                    values=next(csv.reader([line.decode('utf-8')]))
                    if len(values)!=len(headers):raise ValueError('invalid_selected_row')
                    try:dt.date.fromisoformat(day)
                    except ValueError:
                        exceptions.append({'source_row':source_row,'reason':'invalid_date'});continue
                    if not permno.isdecimal():
                        exceptions.append({'source_row':source_row,'reason':'invalid_permno'});continue
                    if scope=='sample' and day=='2025-12-31' and values[ix.get('SecuritySubType',0)]=='ETF' and len(selected_etfs)<2:selected_etfs.add(permno)
                    quote=[v for h,v in zip(headers,values) if not h.startswith('Dis')]
                    raw_hash=hashlib.sha256(json.dumps(values).encode()).hexdigest()
                    quote_hash=hashlib.sha256(json.dumps(quote).encode()).hexdigest()
                    sample_bytes+=len(line);buffer_bytes+=len(line)
                    if scope=='sample' and sample_bytes>256*1024**2:raise ValueError('sample_byte_limit_256MiB')
                    rows.append(values+[batch,fingerprint,member,source_row,sequence,raw_hash,quote_hash]);count+=1
                    if scope=='sample' and count>200000:raise ValueError('sample_row_limit_exceeded')
                    if len(rows)>=10000 or buffer_bytes>=8*1024**2:flush();progress({'scanned':scanned,'selected':count})
                flush()
        try:
            scan()
            if scope=='sample' and not bounded_source and selected_etfs:scan(True)
            if not count:raise ValueError('no_selected_market_rows')
        finally:con.close()
        batch_info={'id':batch,'sequence':sequence,'sha256':fingerprint,'profile':profile,'source_name':Path(source).name,'member':source_member,'scope':scope,'source_rows_scanned':scanned,'selected_raw_rows':count,'fields':headers,'files':files,'exceptions':exceptions,'sample_mode':'bounded_source' if bounded_source else 'representative_filter' if scope=='sample' else 'full_input','sample_windows':WINDOWS if scope=='sample' and not bounded_source else None,'sample_snapshots':sorted(SNAPSHOTS) if scope=='sample' else None,'extra_etfs':sorted(selected_etfs),'imported_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        generation=uuid.uuid4().hex
        manifest={**(old or {}),'generation':generation,'policy_version':POLICY,'scope':scope if not old else scope if all(b['scope']==scope for b in old['batches']) else 'mixed','batches':(old['batches'] if old else [])+[batch_info]}
        (store.root/'generations').mkdir(exist_ok=True)
        atomic_json(store.root/'generations'/f'{generation}.json',manifest)
        with store.connect(generation) as (c,_):
            summary=c.execute('SELECT count(*),count(DISTINCT PERMNO),min(DlyCalDt),max(DlyCalDt),count(*) FILTER(WHERE _quote_conflict) FROM daily').fetchone()
            event_count=c.execute('SELECT count(*) FROM events').fetchone()[0]
        manifest['summary']={'daily_rows':summary[0],'securities':summary[1],'date_min':summary[2],'date_max':summary[3],'conflicting_days':summary[4],'event_rows':event_count,'raw_rows':sum(b['selected_raw_rows'] for b in manifest['batches'])}
        manifest['import_measurements']={'seconds':round(time.monotonic()-started,3),'peak_rss_platform_units':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'disk_bytes':sum((store.root/p).stat().st_size for b in manifest['batches'] for p in b['files'])}
        atomic_json(store.root/'generations'/f'{generation}.json',manifest)
        progress({'complete':True,**manifest['summary']})
        atomic_json(store.root/'active.json',{'generation':generation})
        return manifest
    except BaseException:
        if folder:
            import shutil
            shutil.rmtree(folder,ignore_errors=True)
        raise
    finally:lock.unlink(missing_ok=True)
