"""Validate a fresh real container, upload/import/query, then recreate with same volume.
All values are synthetic. No local research data or user credentials are read.
"""
import argparse
import csv
import io
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid

parser=argparse.ArgumentParser();parser.add_argument('--image',default='financial-api:0.3.1');parser.add_argument('--port',type=int,default=18769);args=parser.parse_args()
name='financial-smoke-'+uuid.uuid4().hex[:10];volume=name+'-data';token=secrets.token_urlsafe(24);base=f'http://127.0.0.1:{args.port}'

def docker(*a):return subprocess.check_output(['docker',*map(str,a)],text=True).strip()
def request(path,payload=None,method=None,raw=None,authenticated=True):
    headers={'Authorization':'Bearer '+token} if authenticated else {}
    if payload is not None:headers['Content-Type']='application/json';raw=json.dumps(payload).encode()
    req=urllib.request.Request(base+path,data=raw,headers=headers,method=method or ('POST' if raw is not None else 'GET'))
    with urllib.request.urlopen(req,timeout=60) as response:
        body=response.read()
        return json.loads(body) if 'json' in response.headers.get('Content-Type','') else body

def start():
    docker('run','--detach','--name',name,'--publish',f'127.0.0.1:{args.port}:8765','--mount',f'type=volume,src={volume},dst=/data','--env','FINANCIAL_API_TOKEN='+token,args.image)
    for _ in range(80):
        try:request('/healthz',authenticated=False);return
        except (OSError,urllib.error.URLError):time.sleep(.5)
    raise RuntimeError('container_did_not_start')

def upload(filename,body,kind):
    item=request('/v1/admin/uploads',dict(name=filename,size=len(body)))
    request('/v1/admin/uploads/'+item['id']+'/chunks?offset=0',method='PUT',raw=body)
    preview=request('/v1/admin/uploads/'+item['id']+'/finish',{})
    assert preview['preview']['kind']==kind
    job=request('/v1/admin/imports',dict(upload_id=item['id'],kind=kind,scope='full'))
    for _ in range(100):
        state=next(x for x in request('/v1/admin/imports')['rows'] if x['id']==job['job_id'])
        if state['status'] in {'complete','failed'}:break
        time.sleep(.2)
    assert state['status']=='complete',state

def csv_bytes(rows):
    text=io.StringIO();writer=csv.DictWriter(text,list(rows[0]));writer.writeheader();writer.writerows(rows);return text.getvalue().encode()

try:
    start();status=request('/v1/status');assert status['status']=='not_initialized' and status['current_rows']==0
    assert request('/v1/sec/status')['status']=='not_initialized'
    assert request('/v1/dictionary?limit=1000')['total']==679
    assert request('/v1/sec/mappings?limit=1000')['total']==679
    assert request('/v1/market/status')['status']=='not_initialized'
    assert b'onboarding' in request('/ui',authenticated=False)
    try:request('/v1/status',authenticated=False);raise AssertionError('auth_not_enforced')
    except urllib.error.HTTPError as exc:assert exc.code==401
    rows=[]
    for q,end in [(1,'2025-03-31'),(2,'2025-06-30'),(3,'2025-09-30'),(4,'2025-12-31')]:
        rows.append(dict(gvkey='900001',cik='0000999999',tic='SYN',conm='SYNTHETIC CONTAINER TEST',datadate=end,fyearq='2025',fqtr=str(q),fyr='12',indfmt='INDL',datafmt='STD',consol='C',curcdq='USD',rdq='2026-02-01',prccq='50',ajexq='1',priusa='01',exchg='14',cusip='000001234',niq='10',saleq='100',atq='1000',ltq='500',epsfiq='2',epsfi12='8',epsfiy='8',cshoq='100',ceqq='400',seqq='500'))
    upload('synthetic-financial.csv',csv_bytes(rows),'financial')
    query={'company':'900001','as_of':'2025-12-31','mode':'research'}
    result=request('/v1/metrics',query);assert result['results'][0]['metrics']['pe_ttm']['value']==6.25
    assert request('/v1/status')['current_rows']==4
    market=[dict(PERMNO='90001',PERMCO='90000',SecInfoStartDt='1900-01-01',SecInfoEndDt='2099-12-31',CUSIP='00000123',CUSIP9='000001234',HdrCUSIP='00000123',HdrCUSIP9='000001234',Ticker='SYN',TradingSymbol='SYN',IssuerNm='SYNTHETIC TEST',ShareClass='A',SecurityType='EQTY',SecuritySubType='COM',ShareType='NS',PrimaryExch='Q',ConditionalType='RW',TradingStatusFlg='A',DlyCalDt=day,DlyPrevDt=prev,DlyPrc='50',DlyClose='50',DlyOpen='50',DlyLow='49',DlyHigh='51',DlyVol='100',DlyRet='0',DlyRetx='0',DlyRetMissFlg='NA',DlyRetDurFlg='D1',DlyFacPrc='1',DlyDelFlg='N',DlyPrcFlg='TR',DisSeqNbr='',DisType='',DisDivAmt='',DisFacPr='',DisFacShr='') for day,prev in [('2025-12-30','2025-12-29'),('2025-12-31','2025-12-30')]]
    upload('synthetic-market.csv',csv_bytes(market),'market')
    assert request('/v1/market/status')['summary']['daily_rows']==2
    assert request('/v1/market/metrics',dict(security='90001',as_of='2025-12-31',mode='research'))['metrics']['pe_ttm']['value']==6.25
    artifact=request('/v1/export',dict(as_of='2025-12-31',mode='research',filters=[dict(metric='pe_ttm',op='gt',value=0)]))
    assert b'900001' in request('/v1/exports/'+artifact['export_id'])
    before=request('/v1/status')['database_generation'];docker('stop','--time','20',name);docker('rm',name);start()
    assert request('/v1/status')['database_generation']==before
    assert request('/v1/metrics',query)['results'][0]['metrics']['pe_ttm']['value']==6.25
    assert request('/v1/market/status')['summary']['daily_rows']==2
    history=request('/v1/admin/history');assert history['total']>0
    assert request('/v1/sec/status')['updates']['network_enabled'] is False
    uid=docker('exec',name,'id','-u');assert uid!='0'
    print(json.dumps(dict(image=args.image,synthetic_only=True,empty_start=True,authentication=True,financial_import=True,market_import=True,metrics=True,exports=True,recreated_container_persists=True,non_root_uid=uid)))
finally:
    subprocess.run(['docker','rm','--force',name],capture_output=True)
    subprocess.run(['docker','volume','rm',volume],capture_output=True)
