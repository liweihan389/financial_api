"""Immutable SEC snapshots and evidence, separate from supplier databases."""
from contextlib import contextmanager
import datetime as dt
import calendar
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import time
import threading
from collections import Counter
import uuid
import zipfile
from zoneinfo import ZoneInfo
import duckdb
from .store import Store, dictionaries
from .sec_mapping import RULES, CUTOFF, rules_version, registry, POLICY, VERSION

FORMAL={'10-Q','10-K','20-F','40-F','10-Q/A','10-K/A','20-F/A','40-F/A'}
MAX_JSON=128*1024**2


def canonical(obj):return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(obj):return hashlib.sha256(canonical(obj).encode()).hexdigest()
def cik(value):
    text=str(value).strip()
    if not re.fullmatch(r'\d{1,10}',text) or int(text)==0:raise ValueError('invalid_cik')
    return text.zfill(10)
def iso(value):return dt.date.fromisoformat(str(value))


class RawDocument(dict):
    def __init__(self,body):
        parsed=json.loads(body)
        if not isinstance(parsed,dict):raise ValueError('sec_json_must_be_object')
        super().__init__(parsed);self.raw_bytes=body

def document_identity(source,obj):
    if not isinstance(obj,dict):raise ValueError('invalid_sec_document_structure')
    if 'cik' in obj:
        company=cik(obj['cik']);kind='facts' if 'facts' in obj else 'submissions' if 'filings' in obj else None
    else:
        match=re.fullmatch(r'CIK(\d{10})-submissions-(\d+)\.json',PurePosixPath(source).name)
        if match and 'accessionNumber' in obj:company=match[1];kind='history:'+PurePosixPath(source).name
        else:raise ValueError('invalid_sec_document_identity')
    if kind is None:raise ValueError('invalid_sec_document_kind')
    return company,kind

def documents(path):
    """Stream ZIP members individually; never extract paths to disk."""
    path=Path(path)
    if path.suffix.lower()=='.json':
        if path.stat().st_size>MAX_JSON:raise ValueError('sec_json_size_limit')
        yield path.name,RawDocument(path.read_bytes());return
    if path.suffix.lower()!='.zip':raise ValueError('sec_requires_json_or_zip')
    with zipfile.ZipFile(path) as z:
        names=set();size=0
        for member in z.infolist():
            name=member.filename;p=PurePosixPath(name)
            if name.startswith('__MACOSX/') or member.is_dir() or p.name.startswith('._'):continue
            if p.is_absolute() or '..' in p.parts or '\\' in name:raise ValueError('unsafe_sec_archive_member')
            if not name.lower().endswith('.json'):continue
            if name in names or member.file_size>MAX_JSON:raise ValueError('sec_archive_member_limit_or_duplicate')
            names.add(name);size+=member.file_size
            if size>40*1024**3:raise ValueError('sec_archive_uncompressed_limit')
            with z.open(member) as f:
                body=f.read(MAX_JSON+1)
                if len(body)>MAX_JSON:raise ValueError('sec_json_size_limit')
                yield name,RawDocument(body)
        if not names:raise ValueError('no_sec_json_members')


def probe(path):
    count=0;first=None
    for name,obj in documents(path):
        document_identity(name,obj);count+=1;first=first or name
    return dict(kind='sec',members=count,member=first,header=['cik','facts / filings'])


def identities(con):
    cols={r[0] for r in con.execute('DESCRIBE financial_current').fetchall()}
    if 'cik' not in cols:return {},[dict(code='missing_cik_field')]
    rows=con.execute("SELECT DISTINCT gvkey,cik FROM financial_current WHERE gvkey IS NOT NULL").fetchall()
    bykey={};bycik={};issues=[]
    for g,c in rows:
        try:c=cik(c)
        except ValueError:
            if g not in bykey:bykey[g]=set()
            continue
        bykey.setdefault(g,set()).add(c);bycik.setdefault(c,set()).add(g)
    valid={}
    for g,cs in bykey.items():
        if len(cs)==1 and len(bycik[next(iter(cs))])==1:valid[next(iter(cs))]=g
        else:issues.append(dict(gvkey=g,code='missing_or_conflicting_cik',candidates=sorted(cs)))
    return valid,issues


def submission_reports(obj):
    out={}
    for block in [obj.get('filings',{}).get('recent',{})]+obj.get('_historical_filings',[]):
        accessions=block.get('accessionNumber',[])
        for i,acc in enumerate(accessions):
            out[acc]={k:(block.get(k,[])[i] if i<len(block.get(k,[])) else None) for k in ['acceptanceDateTime','filingDate','reportDate','form','isXBRL']}
    return out


def visibility(fact, report):
    accepted=report.get('acceptanceDateTime')
    if accepted:
        try:
            value=dt.datetime.fromisoformat(accepted.replace('Z','+00:00'))
            if value.tzinfo is None:value=value.replace(tzinfo=ZoneInfo('America/New_York'))
            return value.astimezone(ZoneInfo('America/New_York')).date().isoformat(),'sec_acceptance_time',accepted
        except ValueError:pass
    return (iso(fact['filed'])+dt.timedelta(days=1)).isoformat(),'sec_filing_date_plus_one_day',None


def fiscal(end, metadata, context):
    fye=str(metadata.get('fiscalYearEnd') or '')
    month=int(fye[:2]) if re.fullmatch(r'\d{4}',fye) and 1<=int(fye[:2])<=12 else int(context.get('fyr') or 12)
    end=iso(end)
    # 52/53-week issuers may close a fiscal quarter just across a month/year.
    # Use a nominal fiscal calendar only when within seven days; otherwise the
    # candidate is marked inferred by the caller.
    nominal=[]
    for y in range(end.year-1,end.year+2):
        for m in range(1,13):
            if (m-month)%3==0:nominal.append(dt.date(y,m,calendar.monthrange(y,m)[1]))
    nearest=min(nominal,key=lambda d:abs((d-end).days))
    if abs((nearest-end).days)<=7:end=nearest
    # Compustat names Jan-May fiscal years after the year in which they start.
    endyear=end.year+(1 if end.month>month else 0)
    year=endyear-(1 if month<=5 else 0)
    q=((end.month-month-1)%12)//3+1
    return year,q,month


class SecStore:
    _identity_cache={}
    _identity_lock=threading.Lock()
    def __init__(self,root):
        self.vendor=Store(root);self.root=self.vendor.root/'sec';self.root.mkdir(exist_ok=True,parents=True)
        (self.root/'raw').mkdir(exist_ok=True)
    def active(self):
        pointer=self.root/'active.json'
        return json.loads(pointer.read_text()) if pointer.exists() else None
    @contextmanager
    def connect(self,generation=None):
        active=self.active();name=generation or (active['sec_generation'] if active else None)
        if name is None:yield None;return
        path=(self.root/name).resolve()
        if path.parent!=self.root or path.suffix!='.duckdb' or not path.is_file():raise ValueError('unknown_sec_generation')
        c=duckdb.connect(str(path),read_only=True)
        try:
            from .telemetry import track_version
            track_version('sec_generation',name);yield c
        finally:c.close()
    def identity_audit(self,generation=None):
        if generation is None and not self.vendor.is_initialized():
            return dict(compustat_generation=None,eligible_ciks=0,identity_issue_companies=0,requires_financial_import=True),[]
        name=generation or self.vendor.active_path().name
        with self._identity_lock:
            if name not in self._identity_cache:
                with self.vendor.connect(name) as c:
                    valid,issues=identities(c)
                    self._identity_cache[name]=(valid,issues)
                    while len(self._identity_cache)>3:self._identity_cache.pop(next(iter(self._identity_cache)))
            valid,issues=self._identity_cache[name]
        return dict(compustat_generation=name,eligible_ciks=len(valid),identity_issue_companies=len(issues)),issues
    def mapping(self):
        if not self.vendor.is_initialized():
            schema=json.loads(Path(__file__).with_name('compustat_schema.json').read_text())
            return registry(schema)
        with self.vendor.connect() as c:return registry(dictionaries(c))
    def _init(self,c):
        c.execute('CREATE TABLE IF NOT EXISTS documents(sha VARCHAR PRIMARY KEY,cik VARCHAR,kind VARCHAR,payload VARCHAR,source VARCHAR)')
        c.execute('CREATE TABLE IF NOT EXISTS heads(cik VARCHAR,kind VARCHAR,sha VARCHAR,PRIMARY KEY(cik,kind))')
        c.execute('CREATE TABLE IF NOT EXISTS facts(id VARCHAR PRIMARY KEY,cik VARCHAR,namespace VARCHAR,tag VARCHAR,unit VARCHAR,start_date VARCHAR,end_date VARCHAR,value DOUBLE,accession VARCHAR,filed VARCHAR,form VARCHAR,payload VARCHAR)')
        c.execute('CREATE TABLE IF NOT EXISTS snapshot_facts(sha VARCHAR,id VARCHAR,PRIMARY KEY(sha,id))')
        c.execute('CREATE TABLE IF NOT EXISTS observations(cik VARCHAR,gvkey VARCHAR,period VARCHAR,field VARCHAR,value DOUBLE,currency VARCHAR,quality VARCHAR,available VARCHAR,accession VARCHAR,evidence VARCHAR)')
        c.execute('CREATE TABLE IF NOT EXISTS issues(cik VARCHAR,gvkey VARCHAR,period VARCHAR,field VARCHAR,code VARCHAR,evidence VARCHAR)')
        c.execute('CREATE TABLE IF NOT EXISTS manifest(payload VARCHAR)')
        c.execute('CREATE TABLE IF NOT EXISTS mapping_rules(version VARCHAR PRIMARY KEY,payload VARCHAR)')
        c.execute('INSERT OR IGNORE INTO mapping_rules VALUES(?,?)',[rules_version(),canonical(dict(rules=RULES,policy=POLICY,implementation=VERSION))])
    def import_file(self,path,progress=None):return self.import_documents(documents(path),progress)
    def import_documents(self,docs,progress=None,reclean=False):
        started=time.monotonic()
        with (self.root/'import.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('sec_import_already_running')
            old=self.active();name='sec-'+uuid.uuid4().hex+'.duckdb';work=self.root/('building-'+name)
            if old:shutil.copy2(self.root/old['sec_generation'],work)
            c=duckdb.connect(str(work));c.execute("SET memory_limit='1GB'");c.execute('SET threads=2');self._init(c);changed=set();total=0;skipped=0
            try:
                with self.vendor.connect() as vendor:
                    universe,identity_issues=identities(vendor)
                    vendor_generation=Path(vendor.execute('PRAGMA database_list').fetchone()[2]).name
                    c.execute("DELETE FROM issues WHERE code='missing_or_conflicting_cik'")
                    for item in identity_issues:c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[None,item.get('gvkey'),None,None,item['code'],canonical(item)])
                    for source,obj in docs:
                        company,kind=document_identity(source,obj)
                        if company not in universe:
                            c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,None,None,None,'unknown_or_conflicting_cik',canonical({'source':source})]);skipped+=1;continue
                        if not kind.startswith('history:') and not isinstance(obj['facts' if kind=='facts' else 'filings'],dict):raise ValueError('invalid_sec_document_structure')
                        body=getattr(obj,'raw_bytes',canonical(obj).encode());sha=hashlib.sha256(body).hexdigest();head=c.execute('SELECT sha FROM heads WHERE cik=? AND kind=?',[company,kind]).fetchone()
                        if head and head[0]==sha:continue
                        raw=self.root/'raw'/f'{sha}.json.gz'
                        if not raw.exists():raw.write_bytes(gzip.compress(body,mtime=0))
                        c.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?)',[sha,company,kind,body.decode('utf-8-sig'),source])
                        c.execute('INSERT OR REPLACE INTO heads VALUES(?,?,?)',[company,kind,sha]);changed.add(company);total+=1
                        if kind=='facts':
                            if not obj['facts'] and c.execute('SELECT count(*) FROM observations WHERE cik=?',[company]).fetchone()[0]:raise ValueError('sec_unexpected_empty_snapshot')
                            for ns,tags in obj['facts'].items():
                                if ns not in {'us-gaap','ifrs-full','dei','srt'}:continue
                                if not isinstance(tags,dict):raise ValueError('invalid_sec_tags')
                                for tag,detail in tags.items():
                                    for unit,values in detail.get('units',{}).items():
                                        for f in values:
                                            if not isinstance(f,dict):raise ValueError('invalid_sec_fact')
                                            try:
                                                val=float(f['val']);iso(f['end']);iso(f['filed']);iso(f['start']) if f.get('start') else None
                                                if not math.isfinite(val) or not re.fullmatch(r'\d{10}-\d{2}-\d{6}',str(f.get('accn',''))):raise ValueError()
                                            except (ValueError,TypeError,KeyError):
                                                c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,universe[company],f.get('end'),tag,'invalid_fact',canonical(f)]);continue
                                            fid=digest([company,ns,tag,unit,f])
                                            c.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[fid,company,ns,tag,unit,f.get('start'),f['end'],val,f['accn'],f['filed'],f.get('form'),canonical(f)])
                                            c.execute('INSERT OR IGNORE INTO snapshot_facts VALUES(?,?)',[sha,fid])
                        if progress:progress(dict(documents=total,company=company,stage='raw_import'))
                    if reclean:
                        changed.update(r[0] for r in c.execute("SELECT DISTINCT cik FROM heads").fetchall() if r[0] in universe)
                    if not changed and not skipped:
                        c.close();work.unlink();return {'status':'unchanged','sec_generation':old['sec_generation'] if old else None,'documents':0}
                    for company in changed:
                        g=universe[company]
                        context=vendor.execute('SELECT to_json(t) FROM (SELECT * FROM metric_inputs WHERE gvkey=? ORDER BY datadate DESC LIMIT 1) t',[g]).fetchone()
                        context=json.loads(context[0]) if context else {}
                        self._normalize(c,company,g,context)
                    count=c.execute('SELECT count(*) FROM observations').fetchone()[0]
                    # Structural assertions precede publication. Any failure leaves old pointer.
                    if c.execute('SELECT count(*) FROM observations WHERE period<=? OR NOT isfinite(value)',[CUTOFF]).fetchone()[0]:raise ValueError('sec_batch_consistency_validation_failed')
                    report=dict(sec_generation=name,rule_version=rules_version(),compustat_generation=vendor_generation,cutoff=CUTOFF,documents=total,changed_companies=len(changed),skipped_companies=skipped,observations=count,issues=c.execute('SELECT count(*) FROM issues').fetchone()[0],seconds=round(time.monotonic()-started,3),created=dt.datetime.now(dt.timezone.utc).isoformat())
                    c.execute('DELETE FROM manifest');c.execute('INSERT INTO manifest VALUES(?)',[canonical(report)])
                    c.close();os.replace(work,self.root/name)
                    report['database_bytes']=(self.root/name).stat().st_size
                    # Enrollment is operational; complete it before switching the active pointer.
                    self.enroll(changed)
                    temp=self.root/'active.tmp';temp.write_text(canonical(report));os.replace(temp,self.root/'active.json')
                    return report
            except BaseException:
                c.close();work.unlink(missing_ok=True);raise
    def enroll(self,companies):
        with sqlite3.connect(self.vendor.root/'operations.sqlite3',timeout=30) as c:
            c.execute('CREATE TABLE IF NOT EXISTS sec_enrolled(cik TEXT PRIMARY KEY,filing_fingerprint TEXT,last_checked TEXT,last_error TEXT)')
            c.executemany('INSERT OR IGNORE INTO sec_enrolled(cik) VALUES(?)',[(company,) for company in companies])
    def _normalize(self,c,company,gvkey,context):
        c.execute('DELETE FROM observations WHERE cik=?',[company]);c.execute("DELETE FROM issues WHERE cik=? AND code NOT IN ('invalid_fact')",[company])
        sub=c.execute("SELECT d.payload FROM documents d JOIN heads h ON d.sha=h.sha WHERE h.cik=? AND h.kind='submissions'",[company]).fetchone()
        metadata=json.loads(sub[0]) if sub else {}
        historical=[json.loads(row[0]) for row in c.execute("SELECT d.payload FROM documents d JOIN heads h ON d.sha=h.sha WHERE h.cik=? AND h.kind LIKE 'history:%'",[company]).fetchall()]
        metadata.setdefault('_historical_filings',[]).extend(historical)
        reports=submission_reports(metadata)
        cursor=c.execute("SELECT f.* FROM facts f JOIN snapshot_facts s ON f.id=s.id JOIN heads h ON s.sha=h.sha WHERE h.cik=? AND h.kind='facts'",[company])
        cols=[d[0] for d in cursor.description];facts=[dict(zip(cols,r)) for r in cursor.fetchall()]
        company_policy=POLICY.get('companies',{}).get(company,{})
        bytag={}
        for f in facts:bytag.setdefault(f['namespace']+':'+f['tag'],[]).append(f)
        for field,r in RULES.items():
            if field=='ifrs_income_candidate':continue
            annual=field.endswith('y')
            for tag in r['tags']:
                for f in bytag.get(tag,[]):
                    if f['end_date']<=CUTOFF:continue
                    unit=f['unit'];currency=unit.split('/')[0] if r['unit']!='shares' else None
                    good=bool(re.fullmatch(r'[A-Z]{3}',unit)) if r['unit']=='money' else bool(re.fullmatch(r'[A-Z]{3}/shares',unit)) if r['unit']=='eps' else unit=='shares'
                    if r['unit']=='shares' and f['value']<=0:
                        c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,'nonpositive_share_count',canonical(f)]);continue
                    if not good:
                        c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,'unit_conflict',canonical(f)]);continue
                    span=(iso(f['end_date'])-iso(f['start_date'])).days+1 if f['start_date'] else None
                    kind='instant' if span is None else 'quarter' if 60<=span<=120 else 'half_year' if 150<=span<=210 else 'annual' if 330<=span<=400 else 'cumulative' if 210<span<330 else 'unsupported_duration'
                    if r['kind']=='instant' and span is not None:continue
                    if r['kind']=='duration' and span is None:continue
                    report=reports.get(f['accession'],{})
                    if report.get('form') and report['form']!=f['form']:
                        c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,'filing_form_conflict',canonical(dict(fact=f,submission=report))]);continue
                    available,visibility_source,accepted=visibility(json.loads(f['payload']),report)
                    quality=r.get('tag_quality',{}).get(tag,r['quality']);reasons=[];inputs=[f['id']];formula='reported_value';value=f['value'];start=f['start_date']
                    formal=f['form'] in FORMAL or f['form'] in {'6-K','6-K/A'} and bool(company_policy.get('periodic_6k_accessions',{}).get(f['accession']))
                    scale=company_policy.get('share_scale_by_period',{}).get(f['end_date'],{})
                    try:factor=float(scale.get('ajex_equivalent',0))
                    except (TypeError,ValueError):factor=0
                    share_verified=bool(scale.get('evidence')) and math.isfinite(factor) and factor>0
                    if quality=='inferred':reasons.append('semantic_mapping_requires_review')
                    if not formal:
                        quality='inferred';reasons.append('preliminary_or_unverified_periodic_form')
                    year,q,month=fiscal(f['end_date'],metadata,context)
                    if not metadata.get('fiscalYearEnd'):
                        quality='inferred';reasons.append('fiscal_calendar_from_supplier_context_unverified')
                    if report.get('reportDate') and f['end_date']!=report['reportDate']:
                        quality='inferred';reasons.append('comparative_period_in_later_filing')
                    nearest_days=min(abs((iso(f['end_date'])-dt.date(y,m,calendar.monthrange(y,m)[1])).days) for y in range(iso(f['end_date']).year-1,iso(f['end_date']).year+2) for m in range(1,13) if (m-month)%3==0)
                    if nearest_days>7:
                        quality='inferred';reasons.append('irregular_fiscal_period_calendar_unverified')
                    if not share_verified and r['unit'] in {'eps','shares'} and (context.get('adrrq') is not None or context.get('priusa')=='90'):
                        quality='inferred';reasons.append('sec_ads_equivalent_assumption_unverified')
                    if annual:
                        # YTD retains actual span; annual EPS usable only on complete FY.
                        if kind not in {'quarter','half_year','annual','cumulative'}:continue
                        if r['unit']=='eps' and kind!='annual':continue
                    elif r['kind']=='duration' and kind!='quarter':
                        if not r['additive'] or kind not in {'half_year','annual','cumulative'}:continue
                        prior=[p for p in bytag.get(tag,[]) if p['start_date']==f['start_date'] and p['unit']==unit and p['end_date']<f['end_date'] and 60<=(iso(f['end_date'])-iso(p['end_date'])).days<=120 and p['filed']<=f['filed'] and (p['filed']!=f['filed'] or p['accession']==f['accession'])]
                        prior.sort(key=lambda p:(p['accession']==f['accession'],p['filed']),reverse=True)
                        if not prior:continue
                        p=prior[0]
                        if len([x for x in prior if x['accession']==p['accession'] and x['end_date']==p['end_date'] and x['value']!=p['value']]):continue
                        prev_available,_,_=visibility(json.loads(p['payload']),reports.get(p['accession'],{}));available=max(available,prev_available)
                        value-=p['value'];start=(iso(p['end_date'])+dt.timedelta(days=1)).isoformat();kind='quarter';inputs.append(p['id']);formula='current_cumulative - prior_cumulative'
                        if p['accession']!=f['accession']:
                            quality='inferred';reasons.append('cross_filing_cumulative_difference_restatement_unverified')
                    if kind=='unsupported_duration':continue
                    if not annual and kind=='quarter' and q not in {1,2,3,4}:continue
                    scaled=value/(1e6 if r['unit'] in {'money','shares'} else 1)
                    evidence=dict(fact_ids=inputs,formula=formula,tag=tag,unit=unit,original_value=f['value'],normalization_scale=1e-6 if r['unit'] in {'money','shares'} else 1,period_type=kind,start=start,end=f['end_date'],fyearq=year,fqtr=q,fyr=month,form=f['form'],formal=formal,accepted=accepted,release_source=visibility_source,semantic=r['semantic'],reasons=reasons,rule_version=rules_version(),tag_priority=r['tags'].index(tag),sec_share_scale_verified=share_verified,sec_adjustment_factor=factor if share_verified else None,share_scale_evidence=scale.get('evidence'),periodic_form_evidence=company_policy.get('periodic_6k_accessions',{}).get(f['accession']))
                    conflicting=c.execute('SELECT value,accession,evidence FROM observations WHERE cik=? AND period=? AND field=? AND currency IS NOT DISTINCT FROM ? AND accession=?',[company,f['end_date'],field,currency,f['accession']]).fetchall()
                    if any(row[0]!=scaled and json.loads(row[2])['tag']==tag for row in conflicting):
                        c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,'conflicting_reported_values',canonical(dict(candidate=evidence,other_accessions=[x[1] for x in conflicting]))])
                    c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,scaled,currency,quality,available,f['accession'],canonical(evidence)])
                    if quality!='trusted':c.execute('INSERT INTO issues VALUES(?,?,?,?,?,?)',[company,gvkey,f['end_date'],field,'inferred_field',canonical(evidence)])
    def status(self):
        active=self.active();coverage,_=self.identity_audit();mapped=Counter(r['status'] for r in self.mapping())
        with self.connect() as c:
            return dict(identity_coverage=coverage,mapping_coverage=dict(mapped),status='ready' if active else 'not_initialized',manifest=active,cutoff=CUTOFF,rule_version=active['rule_version'] if active else rules_version(),configured_rule_version=rules_version(),reclean_required=bool(active and active['rule_version']!=rules_version()),companies=c.execute('SELECT count(DISTINCT cik) FROM heads').fetchone()[0] if c else 0,facts=c.execute('SELECT count(*) FROM facts').fetchone()[0] if c else 0,observations=c.execute('SELECT count(*) FROM observations').fetchone()[0] if c else 0,raw_bytes=sum(p.stat().st_size for p in (self.root/'raw').glob('*.gz')))
    def inspect(self,table='issues',generation=None,company=None,field=None,code=None,limit=100,offset=0):
        if table not in {'issues','facts'} or not 1<=limit<=1000 or offset<0:raise ValueError('invalid_sec_inspection')
        with self.connect(generation) as c:
            if c is None:
                if table=='facts':return dict(rows=[],total=0)
                _,issues=self.identity_audit()
                rows=[dict(cik=None,gvkey=r.get('gvkey'),period=None,field='cik',code=r['code'],evidence=r) for r in issues if (not code or r['code']==code) and not company and (not field or field=='cik')]
                return dict(rows=rows[offset:offset+limit],total=len(rows))
            where=[];args=[]
            for key,value in [('cik',company),('field' if table=='issues' else 'tag',field),('code',code if table=='issues' else None)]:
                if value is not None:where.append(key+'=?');args.append(value)
            cond=' WHERE '+' AND '.join(where) if where else ''
            total=c.execute('SELECT count(*) FROM '+table+cond,args).fetchone()[0]
            cur=c.execute('SELECT * FROM '+table+cond+' LIMIT ? OFFSET ?',args+[limit,offset]);cols=[d[0] for d in cur.description]
            rows=[dict(zip(cols,r)) for r in cur.fetchall()]
            for r in rows:
                if table=='facts':
                    r['source_documents']=[dict(sha256=x[0],source=x[1],raw_path=str(self.root/'raw'/(x[0]+'.json.gz'))) for x in c.execute('SELECT d.sha,d.source FROM snapshot_facts s JOIN documents d ON s.sha=d.sha WHERE s.id=?',[r['id']]).fetchall()]
                for k in ['evidence','payload']:
                    if k in r:r[k]=json.loads(r[k])
            return dict(sec_generation=generation or self.active()['sec_generation'],total=total,rows=rows)
