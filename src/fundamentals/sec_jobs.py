"""Background SEC fetches and the service-owned Asia/Shanghai 20:00 scheduler."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from zoneinfo import ZoneInfo
from .sec_store import cik, identities, canonical, FORMAL, RawDocument
from .sec_mapping import CUTOFF

SAMPLES=['0000320193','0000789019','0001067983','0001318605','0000070858','0000929869','0000811809','0000731766','0001119639','0001326801','0001045810','0000920760','0000034088','0001046179','0001652044','0001577552','0000019617','0000909832','0001018724','0001594805']

class SecForbidden(ValueError):pass

class SecJobs:
    def __init__(self,admin,store):
        self.admin=admin;self.store=store;self.stop=threading.Event();self.thread=None;self.lock=threading.Lock();self.rate_lock=threading.Lock();self.next_request=0.;self.pending=False
        self.settings_path=store.root/'settings.json';self.cache=store.root/'http-cache';self.cache.mkdir(exist_ok=True)
        with admin.connect() as c:
            c.executescript('CREATE TABLE IF NOT EXISTS sec_state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS sec_enrolled(cik TEXT PRIMARY KEY,filing_fingerprint TEXT,last_checked TEXT,last_error TEXT);')
    def settings(self):
        values=json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
        ua=os.environ.get('SEC_USER_AGENT') or values.get('user_agent','')
        valid=bool(re.search(r'[^\s@]+@[^\s@]+\.[^\s@]+',ua)) and len(ua)<=512 and '\n' not in ua and '\r' not in ua
        return dict(user_agent=ua,network_enabled=valid,schedule='20:00',timezone='Asia/Shanghai',requests_per_second=2,sample_network_verified=values.get('sample_network_verified',False))
    def configure(self,user_agent):
        if not isinstance(user_agent,str) or len(user_agent)>512 or '\n' in user_agent or '\r' in user_agent:raise ValueError('invalid_SEC_USER_AGENT')
        if user_agent and not re.search(r'[^\s@]+@[^\s@]+\.[^\s@]+',user_agent):raise ValueError('SEC_USER_AGENT_requires_application_and_contact_email')
        old=json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {};old['user_agent']=user_agent
        tmp=self.settings_path.with_suffix('.tmp');tmp.write_text(canonical(old));os.chmod(tmp,0o600);os.replace(tmp,self.settings_path)
        return self.status()
    def status(self):
        s=self.settings();s.pop('user_agent')
        now=dt.datetime.now(ZoneInfo('Asia/Shanghai'));next_run=now.replace(hour=20,minute=0,second=0,microsecond=0)
        if next_run<=now:next_run+=dt.timedelta(days=1)
        with self.admin.connect() as c:
            enrolled=c.execute('SELECT count(*) FROM sec_enrolled').fetchone()[0]
            state={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM sec_state')}
            retry=[dict(r) for r in c.execute('SELECT cik,last_checked,last_error FROM sec_enrolled WHERE last_error IS NOT NULL')]
        return dict(configuration_source='environment' if os.environ.get('SEC_USER_AGENT') else 'settings_file' if self.settings_path.exists() else 'none',**s,status='enabled' if s['network_enabled'] else 'disabled_missing_contact',enrolled_companies=enrolled,next_run=next_run.isoformat() if s['network_enabled'] and enrolled else None,last_run=state.get('last_run'),last_scheduled_slot=state.get('slot'),retry_companies=retry,running=self.pending)
    def _state(self,key,value):
        with self.admin.connect() as c:c.execute('INSERT OR REPLACE INTO sec_state VALUES(?,?)',[key,canonical(value)])
    def _pause(self,seconds):
        remaining=max(0,seconds)
        while remaining>0:
            chunk=min(30,remaining)
            if self.stop.wait(chunk):raise ValueError('sec_sync_stopped')
            remaining-=chunk
    def fetch(self,url,ttl=300):
        if not self.settings()['network_enabled']:raise ValueError('SEC_USER_AGENT_not_configured_network_disabled')
        if not re.fullmatch(r'https://(?:data\.sec\.gov/(?:api/xbrl/companyfacts/CIK\d{10}\.json|submissions/(?:CIK\d{10}(?:-submissions-\d+)?\.json))|www\.sec\.gov/Archives/edgar/(?:full-index|daily-index)/[A-Za-z0-9./-]+)',url):raise ValueError('sec_url_not_allowlisted')
        key=hashlib.sha256(url.encode()).hexdigest();body=self.cache/(key+'.body');meta=self.cache/(key+'.json')
        info=json.loads(meta.read_text()) if meta.exists() else {}
        if body.exists() and time.time()-info.get('fetched',0)<ttl:return body.read_bytes()
        for attempt in range(4):
            with self.rate_lock:
                self._pause(max(0,self.next_request-time.monotonic()));self.next_request=time.monotonic()+.5
            headers={'User-Agent':self.settings()['user_agent'],'Accept':'application/json,text/plain','Accept-Encoding':'identity'}
            if info.get('etag'):headers['If-None-Match']=info['etag']
            try:
                request=urllib.request.Request(url,headers=headers)
                with urllib.request.urlopen(request,timeout=30) as r:
                    # Redirects must remain on the official SEC hosts.
                    from urllib.parse import urlparse
                    if urlparse(r.url).hostname not in {'data.sec.gov','www.sec.gov'}:raise ValueError('sec_redirect_outside_official_hosts')
                    content=r.read(128*1024**2+1)
                    if len(content)>128*1024**2:raise ValueError('sec_response_size_limit')
                    info=dict(etag=r.headers.get('ETag'),fetched=time.time(),sha256=hashlib.sha256(content).hexdigest())
                    temp=body.with_suffix('.tmp');temp.write_bytes(content);os.replace(temp,body);meta.write_text(canonical(info));return content
            except urllib.error.HTTPError as exc:
                if exc.code==304 and body.exists():
                    info['fetched']=time.time();meta.write_text(canonical(info));return body.read_bytes()
                if exc.code==403:raise SecForbidden('SEC_403_source_stopped_check_contact_and_access_policy') from exc
                if exc.code==429:
                    retry=exc.headers.get('Retry-After','')
                    try:delay=float(retry)
                    except ValueError:
                        try:
                            from email.utils import parsedate_to_datetime
                            delay=(parsedate_to_datetime(retry)-dt.datetime.now(dt.timezone.utc)).total_seconds()
                        except (ValueError,TypeError):delay=2**(attempt+1)
                    if attempt==3:raise ValueError('SEC_429_retry_exhausted') from exc
                    self._pause(max(1,delay));continue
                if exc.code<500 or attempt==3:raise ValueError('SEC_HTTP_'+str(exc.code)) from exc
                self._pause(2**attempt)
            except (TimeoutError,urllib.error.URLError) as exc:
                if attempt==3:raise ValueError('SEC_network_retry_exhausted') from exc
                self._pause(2**attempt)
        raise ValueError('SEC_download_failed')
    def _index_candidates(self,universe,progress):
        today=dt.date.today();start=dt.date.fromisoformat(CUTOFF);qualified=set();forms=FORMAL|{'6-K','6-K/A','8-K','8-K/A'}
        for year in range(start.year,today.year+1):
            for quarter in range(1,5):
                if dt.date(year,(quarter-1)*3+1,1)>today:continue
                progress(dict(stage='filing_index',year=year,quarter=quarter))
                raw=self.fetch(f'https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx',ttl=3600).decode('latin-1')
                for line in raw.splitlines():
                    cells=line.split('|')
                    if len(cells)!=5 or not cells[0].isdigit():continue
                    company=cik(cells[0])
                    if company in universe and cells[2] in forms and cells[3]>CUTOFF:qualified.add(company)
        return qualified
    def start_sync(self,scope='sample',companies=None,scheduled=False):
        if scope not in {'sample','full','initialized'}:raise ValueError('invalid_sec_sync_scope')
        if not self.settings()['network_enabled']:raise ValueError('SEC_USER_AGENT_not_configured_network_disabled')
        if scope=='full' and not self.settings()['sample_network_verified']:raise ValueError('sec_full_initialization_requires_successful_sample_network_validation')
        if companies and len(companies)>1000:raise ValueError('sec_company_selection_limit')
        with self.lock:
            if self.pending:raise ValueError('sec_sync_already_running')
            self.pending=True
        jid=uuid.uuid4().hex
        with self.admin.connect() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',[jid,None,'sec_sync','queued','{}',None,dt.datetime.now(dt.timezone.utc).isoformat(),None])
        def update(status='running',progress=None,error=None,result=None):
            with self.admin.connect() as c:c.execute('UPDATE jobs SET status=?,progress=?,error=?,result=? WHERE id=?',[status,canonical(progress or {}),error,canonical(result) if result else None,jid])
        def run():
            failures=[];success=[];fetched=0;stopped=False
            try:
                update()
                with self.store.vendor.connect() as vendor:universe,_=identities(vendor)
                if companies:
                    selected={cik(x) for x in companies}
                    if not selected<=set(universe):raise ValueError('sec_company_not_in_verified_existing_universe')
                elif scope=='initialized':
                    with self.admin.connect() as c:selected={r[0] for r in c.execute('SELECT cik FROM sec_enrolled')}
                    selected&=set(universe)
                else:selected=set(universe) if scope=='full' else set(SAMPLES)&set(universe)
                if scope!='initialized':
                    qualified=self._index_candidates(selected,lambda p:update(progress=p))
                    # Enrollment includes initialized issuers with no eligible filing yet.
                    with self.admin.connect() as c:
                        for company in selected:c.execute('INSERT OR IGNORE INTO sec_enrolled(cik) VALUES(?)',[company])
                    selected=qualified
                def download():
                    nonlocal fetched,stopped
                    for i,company in enumerate(sorted(selected)):
                        update(progress=dict(stage='company',completed=i,total=len(selected),cik=company))
                        try:
                            submissions=RawDocument(self.fetch(f'https://data.sec.gov/submissions/CIK{company}.json'))
                            if cik(submissions.get('cik'))!=company:raise ValueError('sec_response_identity_mismatch')
                            recent=submissions.get('filings',{}).get('recent',{})
                            fingerprint=hashlib.sha256(canonical(recent).encode()).hexdigest()
                            with self.admin.connect() as c:old=c.execute('SELECT filing_fingerprint,last_error FROM sec_enrolled WHERE cik=?',[company]).fetchone()
                            if scope=='initialized' and old and old[0]==fingerprint and not old[1]:success.append(company);continue
                            # Fetch historic submissions pages only when their filing dates cross the boundary.
                            for item in submissions.get('filings',{}).get('files',[]):
                                if item.get('filingTo','')>CUTOFF and re.fullmatch(r'CIK\d{10}-submissions-\d+\.json',item.get('name','')):
                                    extra=RawDocument(self.fetch('https://data.sec.gov/submissions/'+item['name'],ttl=86400))
                                    yield item['name'],extra
                            facts=RawDocument(self.fetch(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{company}.json'))
                            if cik(facts.get('cik'))!=company:raise ValueError('sec_response_identity_mismatch')
                            yield 'SEC_submissions_'+company,submissions
                            yield 'SEC_companyfacts_'+company,facts
                            fetched+=1;success.append(company)
                            # Checkpoints commit only after the whole immutable batch has published.
                            checkpoints[company]=fingerprint
                        except SecForbidden as exc:
                            failures.append(dict(cik=company,error=str(exc)));stopped=True;break
                        except (ValueError,OSError) as exc:
                            failures.append(dict(cik=company,error=str(exc)))
                checkpoints={};result=self.store.import_documents(download(),lambda p:update(progress=p))
                now=dt.datetime.now(dt.timezone.utc).isoformat()
                with self.admin.connect() as c:
                    for company in success:c.execute('INSERT INTO sec_enrolled VALUES(?,?,?,NULL) ON CONFLICT(cik) DO UPDATE SET filing_fingerprint=coalesce(excluded.filing_fingerprint,sec_enrolled.filing_fingerprint),last_checked=excluded.last_checked,last_error=NULL',[company,checkpoints.get(company),now])
                    for failure in failures:c.execute('INSERT INTO sec_enrolled VALUES(?,NULL,?,?) ON CONFLICT(cik) DO UPDATE SET last_checked=excluded.last_checked,last_error=excluded.last_error',[failure['cik'],now,failure['error']])
                result.update(fetched_companies=fetched,checked_companies=len(success),failures=failures,source_stopped=stopped)
                if scope=='sample' and fetched and not failures:
                    config=json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {};config['sample_network_verified']=True
                    temp=self.settings_path.with_suffix('.tmp');temp.write_text(canonical(config));os.chmod(temp,0o600);os.replace(temp,self.settings_path)
                self._state('last_run',dict(completed=now,result=result,scheduled=scheduled))
                update('partial' if failures else 'complete',progress=dict(complete=True),result=result)
            except BaseException as exc:update('failed',error=str(exc))
            finally:
                with self.lock:self.pending=False
        try:self.admin.pool.submit(run)
        except BaseException:
            self.pending=False;raise
        return dict(job_id=jid,status='queued')
    def tick(self,now=None):
        if not self.settings()['network_enabled'] or self.pending:return
        now=now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
        slot=now.replace(hour=20,minute=0,second=0,microsecond=0)
        if now<slot:slot-=dt.timedelta(days=1)
        with self.admin.connect() as c:
            last=c.execute("SELECT value FROM sec_state WHERE key='slot'").fetchone()
            enrolled=c.execute('SELECT count(*) FROM sec_enrolled').fetchone()[0]
        if not enrolled or last and json.loads(last[0])>=slot.isoformat():return
        self.start_sync('initialized',scheduled=True);self._state('slot',slot.isoformat())
    def start_scheduler(self):
        if self.thread and self.thread.is_alive():return
        def loop():
            while not self.stop.is_set():
                try:self.tick()
                except (ValueError,OSError):pass
                self.stop.wait(30)
        self.thread=threading.Thread(target=loop,name='sec-20-shanghai',daemon=True);self.thread.start()
    def stop_scheduler(self):self.stop.set()
