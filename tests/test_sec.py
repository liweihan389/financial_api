"""All financial values below are SYNTHETIC, not SEC-reported issuer facts."""
import copy
import csv
import datetime as dt
import io
import json
from pathlib import Path
import threading
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
from fundamentals.importer import import_file
from fundamentals.store import Store
from fundamentals.sec_store import SecStore, probe
from fundamentals.sec_combined import FinancialService
from fundamentals.sec_jobs import SecJobs, SAMPLES
from fundamentals.sec_mapping import RULES, rules_version
from fundamentals.admin import Admin
from fundamentals.api import create_app
from test_financial_service import financial_rows, HEADERS

ACC='0000320193-26-000001'

def filing(acc=ACC,day='2026-04-30',end='2026-03-31',form='10-Q',accepted=True):
    return {'accessionNumber':acc,'filingDate':day,'reportDate':end,'form':form,'isXBRL':1,'acceptanceDateTime':day+'T20:00:00Z' if accepted else None}

def submissions(cik='0000320193',reports=None,fye='1231'):
    reports=reports or [filing()]
    return {'cik':cik,'fiscalYearEnd':fye,'filings':{'recent':{key:[r[key] for r in reports] for key in reports[0]},'files':[]}}

def fact(val,end='2026-03-31',start=None,acc=ACC,filed='2026-04-30',form='10-Q'):
    f={'val':val,'end':end,'accn':acc,'filed':filed,'form':form,'fy':2026,'fp':'Q1'}
    if start:f['start']=start
    return f

def facts(cik='0000320193',entries=None):
    entries=entries or [('Assets','USD',fact(2000e6)),('Liabilities','USD',fact(800e6)),('RevenueFromContractWithCustomerExcludingAssessedTax','USD',fact(200e6,start='2026-01-01')),('NetIncomeLoss','USD',fact(20e6,start='2026-01-01')),('EarningsPerShareDiluted','USD/shares',fact(2,start='2026-01-01')),('CommonStockSharesOutstanding','shares',fact(100e6))]
    out={'cik':cik,'entityName':'SYNTHETIC TEST ONLY','facts':{'us-gaap':{}}}
    for tag,unit,f in entries:out['facts']['us-gaap'].setdefault(tag,{'units':{}})['units'].setdefault(unit,[]).append(f)
    return out

def write_vendor(path,rows):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,HEADERS+['cik']);w.writeheader();w.writerows(rows)

@pytest.fixture
def secdata(tmp_path):
    rows=financial_rows()
    for r in rows:
        year=int(r['fyearq'])+4;r['fyearq']=str(year);r['datadate']=str(dt.date(year,int(r['datadate'][5:7]),int(r['datadate'][8:10])));r['rdq']=str(dt.date.fromisoformat(r['datadate'])+dt.timedelta(days=40));r['cik']='0000320193'
    path=tmp_path/'vendor.csv';write_vendor(path,rows);root=tmp_path/'db';import_file(path,root,progress=lambda _:None)
    store=SecStore(root);return root,store,rows,path

def load(store,obj=None,sub=None):
    return store.import_documents([('sub.json',sub or submissions()),('facts.json',obj or facts())])

def test_boundary_idempotence_raw_and_vendor_untouched(secdata):
    root,store,rows,_=secdata;before=Store(root).active_path().read_bytes()
    obj=facts(entries=[('Assets','USD',fact(9,end='2026-02-28')),('Assets','USD',fact(2000e6)),('Liabilities','USD',fact(800e6))])
    report=load(store,obj);assert report['observations']==2
    assert load(store,obj)['status']=='unchanged'
    assert Store(root).active_path().read_bytes()==before
    assert store.status()['facts']==3 and store.status()['raw_bytes']>0
    result=FinancialService(root).get_financials('AAPL',fields=['atq','ltq','prccq','ajexq'],data_source='sec')
    assert len(result['rows'])==1
    assert result['rows'][0]['atq']==2000 and result['rows'][0]['prccq'] is None and result['rows'][0]['ajexq'] is None


def test_revisions_select_after_visibility_and_old_generation(secdata):
    root,store,*_=secdata;old=load(store)
    acc2='0000320193-26-000002';obj=facts();obj['facts']['us-gaap']['Assets']['units']['USD'].append(fact(3000e6,acc=acc2,filed='2026-06-10',form='10-Q/A'))
    load(store,obj,submissions(reports=[filing(),filing(acc2,'2026-06-10',form='10-Q/A')]))
    svc=FinancialService(root)
    early=svc.get_financials('AAPL',fields=['atq'],data_source='sec',mode='historical_approx',as_of='2026-05-01')['rows']
    later=svc.get_financials('AAPL',fields=['atq'],data_source='sec',mode='historical_approx',as_of='2026-06-11')['rows']
    assert early[0]['atq']==2000 and later[0]['atq']==3000
    fixed=svc.get_financials('AAPL',fields=['atq'],data_source='sec',sec_generation=old['sec_generation'])
    assert fixed['rows'][0]['atq']==2000
    with pytest.raises(ValueError,match='rule_version'):svc.get_metrics('AAPL',data_source='combined',generation=old['compustat_generation'])


def test_whole_compustat_period_priority_after_update(secdata,tmp_path):
    root,store,rows,_=secdata;old=load(store);new=dict(rows[-1]);new.update(datadate='2026-03-31',fyearq='2026',fqtr='1',atq='',ltq='700',rdq='2026-05-02');rows.append(new)
    p=tmp_path/'updated.csv';write_vendor(p,rows);import_file(p,root,progress=lambda _:None)
    svc=FinancialService(root);r=svc.get_financials('AAPL',fields=['datadate','atq','ltq'],start='2026-03-01')['rows']
    assert len(r)==1 and r[0]['atq'] is None and r[0]['ltq']==700 and r[0]['data_source']=='compustat'
    assert store.active()['sec_generation']==old['sec_generation']
    replay=svc.get_financials('AAPL',fields=['atq'],data_source='combined',generation=old['compustat_generation'],sec_generation=old['sec_generation'],rule_version=old['rule_version'],start='2026-03-01')
    assert replay['rows'][0]['atq']==2000


def test_cumulative_diff_and_eps_never_diff(secdata):
    root,store,*_=secdata
    a='0000320193-26-000003'
    entries=[('Assets','USD',fact(2000e6,end='2026-06-30',acc=a,filed='2026-08-01'))]
    for tag,v1,v2 in [('NetIncomeLoss',10e6,25e6),('RevenueFromContractWithCustomerExcludingAssessedTax',100e6,240e6),('EarningsPerShareDiluted',1,2.5)]:
        unit='USD/shares' if tag.startswith('Earnings') else 'USD'
        entries += [(tag,unit,fact(v1,start='2026-01-01',acc=a,filed='2026-08-01')),(tag,unit,fact(v2,end='2026-06-30',start='2026-01-01',acc=a,filed='2026-08-01'))]
    load(store,facts(entries=entries),submissions(reports=[filing(a,'2026-08-01','2026-06-30')]))
    row=FinancialService(root).get_financials('AAPL',fields=['niq','niy','saleq','epsfiq'],start='2026-06-01',data_source='sec')['rows'][0]
    assert row['niq']==15 and row['niy']==25 and row['saleq']==140 and row['epsfiq'] is None
    assert row['field_provenance']['niq']['evidence']['formula']=='current_cumulative - prior_cumulative'
    assert len(row['field_provenance']['niq']['evidence']['fact_ids'])==2


def test_preboundary_evidence_does_not_generate_old_records(secdata):
    root,store,*_=secdata;a='0000320193-26-000003'
    entries=[('NetIncomeLoss','USD',fact(10e6,end='2026-01-31',start='2025-11-01',acc=a,filed='2026-06-01')),('NetIncomeLoss','USD',fact(30e6,end='2026-04-30',start='2025-11-01',acc=a,filed='2026-06-01'))]
    load(store,facts(entries=entries),submissions(reports=[filing(a,'2026-06-01','2026-04-30')],fye='1031'))
    row=FinancialService(root).get_financials('AAPL',fields=['niq','niy'],data_source='sec')['rows']
    assert len(row)==1 and row[0]['datadate']=='2026-04-30' and row[0]['niq']==20


def test_cross_filing_inference_excluded_historical(secdata):
    root,store,*_=secdata;a='0000320193-26-000003';entries=[('Assets','USD',fact(2000e6,end='2026-06-30',acc=a,filed='2026-08-01')),('NetIncomeLoss','USD',fact(10e6,start='2026-01-01')),('NetIncomeLoss','USD',fact(25e6,end='2026-06-30',start='2026-01-01',acc=a,filed='2026-08-01'))]
    load(store,facts(entries=entries),submissions(reports=[filing(),filing(a,'2026-08-01','2026-06-30')]))
    svc=FinancialService(root);r=svc.get_financials('AAPL',fields=['niq','atq'],start='2026-06-01',data_source='sec')['rows'][0]
    h=svc.get_financials('AAPL',fields=['niq','atq'],start='2026-06-01',data_source='sec',mode='historical_approx',as_of='2026-08-10')['rows'][0]
    assert r['niq']==15 and h['niq'] is None and h['atq']==2000


def test_missing_acceptance_and_preliminary_forms(secdata):
    root,store,*_=secdata;load(store,sub=submissions(reports=[filing(accepted=False)]));svc=FinancialService(root)
    assert svc.get_financials('AAPL',data_source='sec',mode='historical_approx',as_of='2026-04-30')['rows']==[]
    assert svc.get_financials('AAPL',data_source='sec',mode='historical_approx',as_of='2026-05-01')['rows'][0]['release_date_source']=='sec_filing_date_plus_one_day'
    entries=[('Assets','USD',fact(2500e6,form='8-K'))];load(store,facts(entries=entries),submissions(reports=[filing(form='8-K')]))
    assert svc.get_financials('AAPL',data_source='sec',mode='historical_approx',as_of='2026-05-01')['rows']==[]
    assert svc.get_financials('AAPL',data_source='sec')['rows'][0]['field_provenance']['atq']['quality']=='inferred'


def test_currency_unit_conflict_and_duplicate_conflicts(secdata):
    root,store,*_=secdata
    entries=[('Assets','USD',fact(2000e6)),('Assets','USD',fact(3000e6)),('Liabilities','USD',fact(800e6)),('EarningsPerShareDiluted','USD',fact(3,start='2026-01-01'))]
    load(store,facts(entries=entries));r=FinancialService(root).get_financials('AAPL',fields=['atq','ltq','epsfiq'],data_source='sec')['rows'][0]
    assert r['atq'] is None and r['epsfiq'] is None and r['ltq']==800
    assert store.inspect(code='unit_conflict')['total']>0


def test_metrics_mixed_sources_and_stale_price(secdata):
    root,store,*_=secdata;load(store);svc=FinancialService(root)
    r=svc.get_metrics('AAPL',as_of='2026-05-02',mode='research')['results'][0]
    assert r['financial_period']=='2026-03-31' and r['metrics']['debt_ratio']['value']==.4
    assert r['metrics']['pe_ttm']['value'] is None and r['metrics']['pe_ttm']['reason']=='stale_price'
    assert r['metrics']['revenue_yoy']['value']==1 and r['metrics']['net_income_yoy']['value']==1
    assert r['metrics']['roe']['numerator']==50
    assert set(r['data_sources'])=={'compustat','sec'}
    assert any(x['data_source']=='sec' for x in r['metrics']['debt_ratio']['input_facts'])
    historical=svc.get_metrics('AAPL',as_of='2026-04-15')['results'][0]
    assert historical['financial_period']=='2025-12-31'


def test_adr_research_assumption_history_and_verified_guard(secdata,tmp_path):
    root,store,rows,_=secdata
    for r in rows:r.update(priusa='90',adrrq='5')
    p=tmp_path/'adr.csv';write_vendor(p,rows);import_file(p,root,progress=lambda _:None);load(store)
    svc=FinancialService(root)
    research=svc.get_financials('AAPL',fields=['epsfiq','cshoq'],data_source='sec')['rows'][0]
    hist=svc.get_financials('AAPL',fields=['epsfiq','cshoq'],data_source='sec',mode='historical_approx',as_of='2026-05-02')['rows'][0]
    assert research['epsfiq']==2 and research['cshoq']==100 and hist['epsfiq'] is None and hist['cshoq'] is None
    r=svc.get_metrics('AAPL',as_of='2026-05-02',mode='research',max_price_age=365)['results'][0]
    assert 'sec_as_reported_eps_and_shares_scale_assumed_unverified' in r['warnings'] and not r['adr_ratio_applied']
    strict=svc.get_metrics('AAPL',as_of='2026-05-02',mode='research',max_price_age=365,share_policy='verified_only')['results'][0]
    assert strict['metrics']['pe_annualized']['value'] is None


def test_atomic_failure_retains_pointer_and_pinned_read(secdata):
    root,store,*_=secdata;old=load(store)
    with store.connect(old['sec_generation']) as c:
        n=c.execute('SELECT count(*) FROM observations').fetchone()[0]
        with pytest.raises(ValueError):store.import_documents([('bad.json',{'cik':'0000320193','facts':[]})])
        assert c.execute('SELECT count(*) FROM observations').fetchone()[0]==n
    assert store.active()['sec_generation']==old['sec_generation']


def test_zip_offline_upload_rest_and_export_consistency(secdata,tmp_path):
    root,store,*_=secdata;p=tmp_path/'sec.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr('companyfacts/CIK0000320193.json',json.dumps(facts()));z.writestr('submissions/CIK0000320193.json',json.dumps(submissions()))
    assert probe(p)['members']==2
    load(store);svc=FinancialService(root)
    with TestClient(create_app(root)) as client:
        assert client.get('/v1/sec/status').json()['updates']['status']=='disabled_missing_contact'
        assert client.post('/v1/sec/sync',json={}).status_code==400
        q=dict(company='AAPL',as_of='2026-05-02',mode='research',data_source='combined')
        assert client.post('/v1/metrics',json=q).json()==svc.get_metrics(**q)
        screen=dict(as_of='2026-05-02',mode='research',filters=[dict(metric='debt_ratio',op='eq',value=.4)])
        response=client.post('/v1/screen',json=screen).json();assert response['total']==1
        exported=client.post('/v1/export',json=screen).json()
        payload=client.get('/v1/exports/'+exported['export_id']).content.decode('utf-8-sig');row=list(csv.DictReader(io.StringIO(payload)))[0]
        assert float(row['debt_ratio'])==.4 and exported['metadata']['sec_generation']==response['sec_generation']
        assert 'sec' in row['input_records'] or 'sec' in row['data_sources']
        upload=client.post('/v1/admin/uploads',json=dict(name='sec.zip',size=p.stat().st_size)).json()
        assert client.put('/v1/admin/uploads/'+upload['id']+'/chunks?offset=0',content=p.read_bytes()).status_code==200
        assert client.post('/v1/admin/uploads/'+upload['id']+'/finish',json={}).json()['preview']['kind']=='sec'
        job=client.post('/v1/admin/imports',json=dict(upload_id=upload['id'],kind='sec',scope='sample')).json();assert 'job_id' in job
        client.app.state.admin.pool.shutdown(wait=True)
        assert client.get('/v1/admin/imports').json()['rows'][0]['status']=='complete'
        assert 'SEC' in client.get('/ui').text


def test_official_mapping_all_target_fields(secdata):
    root,store,*_=secdata;mapping=store.mapping()
    with Store(root).connect() as c:count=c.execute('SELECT count(*) FROM field_dictionary').fetchone()[0]
    assert len(mapping)==count and len({r['field'] for r in mapping})==count
    assert next(r for r in mapping if r['field']=='ajexq')['status']=='not_applicable'
    assert 'epsfxq' not in RULES and 'cshoq' in RULES


def test_unknown_cik_does_not_add_company_and_unsafe_zip(secdata,tmp_path):
    root,store,*_=secdata;report=load(store,facts(cik='0000999999'))
    assert report['skipped_companies']==1 and store.inspect(code='unknown_or_conflicting_cik')['total']==1
    p=tmp_path/'bad.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr('../evil.json',json.dumps(facts()))
    with pytest.raises(ValueError,match='unsafe'):store.import_file(p)


def test_scheduler_disabled_catchup_and_contact_redaction(secdata,monkeypatch):
    root,store,*_=secdata;admin=Admin(root);jobs=SecJobs(admin,store);calls=[]
    monkeypatch.delenv('SEC_USER_AGENT',raising=False)
    jobs.tick(dt.datetime(2026,10,13,21,tzinfo=dt.timezone(dt.timedelta(hours=8))));assert not calls
    jobs.configure('SyntheticTest contact@example.test')
    with admin.connect() as c:c.execute('INSERT OR IGNORE INTO sec_enrolled(cik) VALUES(?)',['0000320193'])
    monkeypatch.setattr(jobs,'start_sync',lambda *a,**kw:calls.append((a,kw)))
    now=dt.datetime(2026,10,13,21,tzinfo=dt.timezone(dt.timedelta(hours=8)))
    jobs.tick(now);jobs.tick(now);assert len(calls)==1
    restarted=SecJobs(admin,store);monkeypatch.setattr(restarted,'start_sync',lambda *a,**kw:calls.append((a,kw)))
    restarted.tick(now+dt.timedelta(days=5));assert len(calls)==2
    assert 'user_agent' not in restarted.status()
    admin.log('REST','/v1/sec/settings',{'user_agent':'SyntheticTest contact@example.test'},200,0)
    assert admin.history()['rows'][0]['parameters']['user_agent']=='[redacted]'
    admin.pool.shutdown(wait=True)


def test_twenty_representative_synthetic_issuers(tmp_path):
    rows=[];docs=[]
    for i,c in enumerate(SAMPLES):
        r=financial_rows()[-1].copy();r.update(gvkey=str(i+1).zfill(6),cik=c,tic='SYN'+str(i),datadate='2025-12-31',fyearq='2025',rdq='2026-02-15',priusa='90' if i%4==0 else '01',adrrq='2' if i%4==0 else '')
        rows.append(r)
        docs+=[('sub-'+c,submissions(c)),('facts-'+c,facts(c))]
    p=tmp_path/'twenty.csv';write_vendor(p,rows);root=tmp_path/'db';import_file(p,root,progress=lambda _:None);store=SecStore(root);report=store.import_documents(docs)
    assert report['changed_companies']==20 and store.status()['companies']==20
    svc=FinancialService(root)
    screen=svc.screen_companies('2026-05-02',filters=[dict(metric='debt_ratio',op='eq',value=.4)])
    assert screen['total']==20
    assert store.inspect(code='inferred_field')['total']>0


def test_ifrs_multicurrency_halfyear_and_noncalendar_fiscal(secdata):
    root,store,*_=secdata
    obj={'cik':'0000320193','facts':{'ifrs-full':{
        'Assets':{'units':{'EUR':[fact(2e9,end='2026-06-30')]}},
        'Liabilities':{'units':{'EUR':[fact(.8e9,end='2026-06-30')]}},
        'Revenue':{'units':{'EUR':[fact(5e8,end='2026-06-30',start='2026-01-01')]}},
        'ProfitLossAttributableToOwnersOfParent':{'units':{'EUR':[fact(5e7,end='2026-06-30',start='2026-01-01')]}}
    }}}
    for tags in obj['facts'].values():
        for tag in tags.values():
            for values in tag['units'].values():
                for f in values:f['form']='20-F';f['filed']='2026-08-01'
    load(store,obj,submissions(reports=[filing(day='2026-08-01',end='2026-06-30',form='20-F')],fye='0630'))
    svc=FinancialService(root);row=svc.get_financials('AAPL',fields=['saleq','saley','niy','epsfiq'],data_source='sec')['rows'][0]
    assert row['saleq'] is None and row['saley']==500 and row['niy']==50 and row['epsfiq'] is None
    assert row['fiscal_year']==2026 and row['fiscal_quarter']==4
    assert row['field_provenance']['saley']['evidence']['period_type']=='half_year'
    hist=svc.get_financials('AAPL',fields=['niy','atq'],data_source='sec',mode='historical_approx',as_of='2026-08-03')['rows'][0]
    assert hist['niy'] is None and hist['atq']==2000
    point=svc.get_metrics('AAPL',as_of='2026-08-03',mode='research',max_price_age=365)['results'][0]
    assert point['metrics']['debt_ratio']['value']==.4 and point['metrics']['pe_annualized']['value'] is None


def test_multicurrency_ambiguity_blocks_metrics(secdata):
    root,store,*_=secdata
    obj=facts(entries=[('Assets','USD',fact(2e9)),('Assets','CAD',fact(3e9)),('Liabilities','USD',fact(.8e9)),('Liabilities','CAD',fact(1.2e9))])
    load(store,obj);svc=FinancialService(root)
    rows=svc.get_financials('AAPL',data_source='sec')['rows'];assert len(rows)==2 and all(r['ambiguous_period'] for r in rows)
    r=svc.get_metrics('AAPL',as_of='2026-05-02',mode='research')['results'][0]
    assert r['metrics']['debt_ratio']['reason']=='ambiguous_financial_period'


def test_submissions_bulk_historical_page_and_raw_bytes(secdata,tmp_path):
    root,store,*_=secdata
    historic={k:v for k,v in submissions()['filings']['recent'].items()}
    main=submissions();main['filings']['recent']={k:[] for k in historic}
    p=tmp_path/'official.zip';raw=json.dumps(facts(),indent=3).encode()
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('CIK0000320193.json',raw)
        z.writestr('sub/CIK0000320193.json',json.dumps(main))
        z.writestr('CIK0000320193-submissions-001.json',json.dumps(historic))
    assert probe(p)['members']==3;store.import_file(p)
    import gzip,hashlib
    assert gzip.decompress((store.root/'raw'/(hashlib.sha256(raw).hexdigest()+'.json.gz')).read_bytes())==raw
    row=FinancialService(root).get_financials('AAPL',data_source='sec',mode='historical_approx',as_of='2026-04-30')['rows'][0]
    assert row['release_date_source']=='sec_acceptance_time'


def test_reclean_rules_and_fifty_three_week_calendar(secdata):
    root,store,*_=secdata;load(store)
    old=store.active()['sec_generation'];new=store.import_documents([],reclean=True)
    assert new['sec_generation']!=old and store.status()['observations']>0
    from fundamentals.sec_store import fiscal
    assert fiscal('2027-01-02',{'fiscalYearEnd':'1231'},{'fyr':12})==(2026,4,12)


def test_retry_rate_cache_and_403(secdata,monkeypatch):
    root,store,*_=secdata;admin=Admin(root);jobs=SecJobs(admin,store);jobs.configure('SyntheticTest test@example.test')
    import urllib.error,urllib.request
    pauses=[];calls=[];monkeypatch.setattr(jobs,'_pause',lambda seconds:pauses.append(seconds))
    class Response:
        url='https://data.sec.gov/submissions/CIK0000320193.json'
        headers={'ETag':'test'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b'{"cik":"0000320193"}'
    def request(req,timeout):
        calls.append(req)
        if len(calls)==1:raise urllib.error.HTTPError(req.full_url,429,'retry',{'Retry-After':'3'},None)
        return Response()
    monkeypatch.setattr(urllib.request,'urlopen',request);url=Response.url
    assert jobs.fetch(url)==b'{"cik":"0000320193"}' and jobs.fetch(url)==b'{"cik":"0000320193"}'
    assert len(calls)==2 and 3 in pauses and any(p>0 for p in pauses if p!=3)
    assert calls[0].get_header('User-agent')=='SyntheticTest test@example.test'
    def forbidden(req,timeout):raise urllib.error.HTTPError(req.full_url,403,'forbidden',{},None)
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)
    with pytest.raises(ValueError,match='403_source_stopped'):jobs.fetch(url,ttl=0)
    assert jobs.settings()['network_enabled']
    with pytest.raises(ValueError,match='allowlisted'):jobs.fetch('https://example.com/data')
    admin.pool.shutdown(wait=True)


def test_partial_fetch_retains_old_company_and_checkpoint(secdata,monkeypatch):
    root,store,*_=secdata;load(store);admin=Admin(root);jobs=SecJobs(admin,store);jobs.configure('SyntheticTest test@example.test')
    with admin.connect() as c:c.execute('INSERT OR IGNORE INTO sec_enrolled(cik) VALUES(?)',['0000320193'])
    old=store.active()['sec_generation']
    monkeypatch.setattr(jobs,'fetch',lambda *a,**k:(_ for _ in ()).throw(ValueError('network_failed')))
    queued=jobs.start_sync('initialized')
    with pytest.raises(ValueError,match='already_running'):jobs.start_sync('initialized')
    admin.pool.shutdown(wait=True)
    job=admin.jobs()['rows'][0]
    assert job['status']=='partial' and job['result']['failures'][0]['cik']=='0000320193'
    assert store.active()['sec_generation']==old and jobs.status()['retry_companies']


def test_future_cumulative_evidence_cannot_be_used_early(secdata):
    root,store,*_=secdata;a='0000320193-26-000003'
    obj=facts(entries=[('Assets','USD',fact(2e9,end='2026-06-30',filed='2026-08-01',acc=a)),('NetIncomeLoss','USD',fact(10e6,start='2026-01-01',filed='2026-09-01')),('NetIncomeLoss','USD',fact(30e6,start='2026-01-01',end='2026-06-30',filed='2026-08-01',acc=a))])
    load(store,obj,submissions(reports=[filing(day='2026-09-01'),filing(a,'2026-08-01','2026-06-30')]))
    row=FinancialService(root).get_financials('AAPL',data_source='sec',mode='historical_approx',as_of='2026-08-02',start='2026-06-01')['rows'][0]
    assert row['niq'] is None


def test_daily_sec_visibility_next_observed_trading_day_and_anchor(secdata,tmp_path):
    root,store,*_=secdata;load(store)
    from test_market import csv_market,market_row
    from fundamentals.market_store import import_market
    from fundamentals.market_service import MarketService
    p=tmp_path/'market.csv';csv_market(p,[market_row('2025-12-31'),market_row('2026-04-30'),market_row('2026-05-01'),market_row('2026-05-04')]);import_market(p,root,scope='full')
    svc=MarketService(root)
    early=svc.daily_metrics('10026','2026-04-30')
    assert early['financial_period']=='2025-12-31'
    hist=svc.daily_metrics('10026','2026-05-01')
    assert hist['financial_period']=='2026-03-31' and hist['release_effective_date']=='2026-05-01'
    assert hist['metrics']['debt_ratio']['value']==.4
    assert hist['metrics']['pe_annualized']['value'] is None and hist['metrics']['pe_annualized']['reason']=='sec_share_and_split_scale_unverified'
    research=svc.daily_metrics('10026','2026-05-01',mode='research')
    assert research['metrics']['pe_annualized']['value']==6.25
    assert research['validation_anchor_period']=='2025-12-31'
    with TestClient(create_app(root)) as client:
        assert client.post('/v1/market/metrics',json=dict(security='10026',as_of='2026-05-01',mode='research')).json()==research


def test_all_mapping_statuses_present_before_sec_initialization(secdata):
    root,store,*_=secdata
    status=store.status();assert status['identity_coverage']['eligible_ciks']==1
    assert sum(status['mapping_coverage'].values())==len(HEADERS)+1


def test_manual_verified_adr_and_periodic_6k_policy(secdata,tmp_path,monkeypatch):
    root,store,rows,_=secdata
    for r in rows:r.update(priusa='90',adrrq='5')
    p=tmp_path/'adr.csv';write_vendor(p,rows);import_file(p,root,progress=lambda _:None)
    from fundamentals.sec_mapping import POLICY
    monkeypatch.setitem(POLICY['companies'],'0000320193',dict(periodic_6k_accessions={ACC:'Synthetic reviewed periodic 6-K evidence'},share_scale_by_period={'2026-03-31':dict(ajex_equivalent=1,evidence='Synthetic ADS and split-scale audit')}))
    obj=facts()
    for tag in obj['facts']['us-gaap'].values():
        for values in tag['units'].values():
            for f in values:f['form']='6-K'
    load(store,obj,submissions(reports=[filing(form='6-K')]))
    svc=FinancialService(root)
    row=svc.get_financials('AAPL',fields=['epsfiq','cshoq'],data_source='sec',mode='historical_approx',as_of='2026-05-02')['rows'][0]
    assert row['epsfiq']==2 and row['field_provenance']['epsfiq']['quality']=='trusted'
    result=svc.get_metrics('AAPL',as_of='2026-05-02',share_policy='verified_only',max_price_age=365)['results'][0]
    # SEC scale is audited; vendor ADR anchor still needs its independent audit.
    assert result['metrics']['pe_annualized']['reason']=='unverified_adr_share_basis'


def test_replaced_sec_still_queryable_explicitly(secdata,tmp_path):
    root,store,rows,_=secdata;load(store)
    new=dict(rows[-1]);new.update(datadate='2026-03-31',fyearq='2026',fqtr='1',atq='9000',rdq='2026-05-02');rows.append(new)
    p=tmp_path/'updated.csv';write_vendor(p,rows);import_file(p,root,progress=lambda _:None)
    svc=FinancialService(root)
    assert svc.get_financials('AAPL',fields=['atq'],start='2026-03-01')['rows'][0]['atq']==9000
    assert svc.get_financials('AAPL',fields=['atq'],start='2026-03-01',data_source='sec')['rows'][0]['atq']==2000


def test_daily_export_retains_sec_input_evidence(secdata,tmp_path):
    root,store,*_=secdata;load(store)
    from test_market import csv_market,market_row
    from fundamentals.market_store import import_market
    from fundamentals.market_service import MarketService
    p=tmp_path/'market.csv';csv_market(p,[market_row('2025-12-31'),market_row('2026-05-01')]);import_market(p,root,scope='full')
    service=MarketService(root);artifact=service.export(securities=['10026'],start='2026-05-01',end='2026-05-01',dataset='daily_metrics')
    row=list(csv.DictReader(Path(artifact['path']).open()))[0]
    assert row['sec_generation']==store.active()['sec_generation'] and float(row['debt_ratio'])==.4
    assert any(r['data_source']=='sec' for r in json.loads(row['input_facts'])['debt_ratio'])


def test_offline_initialization_enrolls_scheduled_scope(secdata):
    root,store,*_=secdata;load(store);admin=Admin(root);jobs=SecJobs(admin,store)
    assert jobs.status()['enrolled_companies']==1 and not jobs.status()['network_enabled']
    admin.pool.shutdown(wait=True)


def test_new_vendor_cik_conflict_blocks_unpinned_sec_but_old_replay_works(secdata,tmp_path):
    root,store,rows,_=secdata;old=load(store)
    new=copy.deepcopy(rows[-1]);new.update(cik='0000789019',datadate='2026-01-31',fyearq='2026',fqtr='1',rdq='2026-03-15');rows.append(new)
    p=tmp_path/'identity-change.csv';write_vendor(p,rows);import_file(p,root,progress=lambda _:None)
    svc=FinancialService(root)
    assert svc.get_financials('AAPL',data_source='sec')['rows']==[]
    replay=svc.get_financials('AAPL',data_source='combined',generation=old['compustat_generation'],sec_generation=old['sec_generation'],rule_version=old['rule_version'],start='2026-03-01')
    assert replay['rows'][0]['atq']==2000
