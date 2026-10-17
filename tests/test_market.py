import csv
import datetime as dt
import json
import math
from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient
from fundamentals.market_store import import_market,MarketStore
from fundamentals.market_service import MarketService
from fundamentals.technical import indicators,return_statistics,aligned
from fundamentals.api import create_app
from fundamentals.importer import import_file
from test_financial_service import financial_rows,write_csv

FIELDS='PERMNO PERMCO SecInfoStartDt SecInfoEndDt HdrCUSIP HdrCUSIP9 CUSIP CUSIP9 Ticker TradingSymbol IssuerNm ShareClass SecurityType SecuritySubType ShareType PrimaryExch TradingStatusFlg ConditionalType DlyCalDt DlyPrevDt DlyPrc DlyPrcFlg DlyClose DlyLow DlyHigh DlyOpen DlyVol DlyRet DlyRetx DlyRetMissFlg DlyRetDurFlg DlyFacPrc DlyDelFlg DisSeqNbr DisType DisDivAmt DisFacPr DisFacShr'.split()
def market_row(day='2020-12-31',**kw):
    return {**dict.fromkeys(FIELDS,''),'PERMNO':'10026','PERMCO':'00001','SecInfoStartDt':'1900-01-01','SecInfoEndDt':'2099-12-31','HdrCUSIP':'00000123','HdrCUSIP9':'000001234','CUSIP':'00000123','CUSIP9':'000001234','Ticker':'AAPL','TradingSymbol':'AAPL','IssuerNm':'Test Apple','ShareClass':'A','SecurityType':'EQTY','SecuritySubType':'COM','ShareType':'NS','PrimaryExch':'Q','ConditionalType':'RW','TradingStatusFlg':'A','DlyCalDt':day,'DlyPrevDt':str(dt.date.fromisoformat(day)-dt.timedelta(days=1)),'DlyPrc':'50','DlyClose':'50','DlyLow':'49','DlyHigh':'51','DlyOpen':'50','DlyVol':'100','DlyRet':'0','DlyRetx':'0','DlyRetMissFlg':'NA','DlyRetDurFlg':'D1','DlyFacPrc':'1','DlyDelFlg':'N','DlyPrcFlg':'TR',**kw}
def csv_market(path,rows):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,FIELDS);w.writeheader();w.writerows(rows)
@pytest.fixture
def market(tmp_path):
    finance=tmp_path/'finance.csv';write_csv(finance,financial_rows());import_file(finance,tmp_path/'db',progress=lambda _:None)
    rows=[market_row('2020-09-30')]+[market_row('2020-12-31',DisSeqNbr='1',DisType='CD',DisDivAmt='0.1'),market_row('2020-12-31',DisSeqNbr='2',DisType='CG',DisDivAmt='0.2')]
    src=tmp_path/'market.csv';csv_market(src,rows);m=import_market(src,tmp_path/'db',scope='full')
    return tmp_path/'db',src,m

def test_day_events_provenance_idempotency_and_version_merge(market,tmp_path):
    root,source,old=market;service=MarketService(root)
    assert old['summary']['raw_rows']==3 and old['summary']['daily_rows']==2 and old['summary']['event_rows']==2
    assert import_market(source,root,scope='full')['already_imported']
    assert service.prices('10026',events=True)['total']==2
    point=service.prices('10026')['rows'][-1]
    assert point['_source_row'] and not point['_quote_conflict']
    changed=tmp_path/'changed.csv';csv_market(changed,[market_row('2020-12-31',DlyPrc='60')]);new=import_market(changed,root,scope='full')
    assert new['generation']!=old['generation']
    assert float(service.prices('10026')['rows'][-1]['DlyPrc'])==60
    assert float(service.prices('10026',generation=old['generation'])['rows'][-1]['DlyPrc'])==50
    assert service.prices('10026')['total']==2

def test_bad_import_preserves_active_and_conflicts_block(market,tmp_path):
    root,_,old=market;bad=tmp_path/'bad.csv';bad.write_text('no,fields\n1,2\n')
    with pytest.raises(ValueError):import_market(bad,root,scope='full')
    assert MarketStore(root).manifest()['generation']==old['generation']
    conflict=tmp_path/'conflict.csv';csv_market(conflict,[market_row(DlyPrc='60'),market_row(DlyPrc='50')]);import_market(conflict,root,scope='full')
    assert MarketService(root).prices('10026')['rows'][-1]['_quote_conflict']
    assert MarketService(root).daily_metrics('10026','2020-12-31',mode='research')['metrics']['pe_ttm']['value'] is None

def test_daily_valuation_publication_stale_currency_and_units(market):
    root,_,_=market;s=MarketService(root)
    point=s.daily_metrics('10026','2020-12-31',mode='research')
    assert point['metrics']['pe_ttm']['value']==6.25
    assert point['release_effective_date'] is None
    assert s.daily_metrics('10026','2020-12-31')['financial_period']=='2020-09-30'
    assert s.daily_metrics('10026','2021-01-10',mode='research')['metrics']['pe_ttm']['reason']=='stale_price'
    with pytest.raises(ValueError):s.daily_metrics('10026','2020-12-31',mode='strict')

def test_known_technical_answers_and_dividend_basis():
    rows=[]
    for i in range(40):
        d=dt.date(2020,1,1)+dt.timedelta(days=i);price=i+10
        row=market_row(str(d),DlyPrc=str(price),DlyHigh=str(price+1),DlyLow=str(price-1),DlyRetx=str(0 if i==0 else price/(price-1)-1),DlyRet=str(0 if i==0 else price/(price-1)-1));row['_quote_conflict']=False;rows.append(row)
    out=indicators(rows,sma=[3],ema_periods=[3],rsi_period=3,atr_period=3,bb_period=3,macd=(2,3,2),windows=[3])
    assert out[-1]['values']['sma_3']==pytest.approx(48,rel=1e-5)
    assert out[-1]['values']['rsi']==100
    assert out[-1]['values']['atr']==pytest.approx(2,rel=1e-5)
    assert out[-1]['values']['momentum_3']==pytest.approx(49/46-1,rel=1e-5)
    split=[market_row('2020-01-01',DlyPrc='100'),market_row('2020-01-02',DlyPrc='50',DlyRetx='0',DlyFacPrc='2')]
    assert aligned(split)[0]==[50,50]
    div=[market_row('2020-01-01',DlyPrc='100'),market_row('2020-01-02',DlyPrc='99',DlyRetx='-0.01',DlyRet='0')]
    assert return_statistics(div)['cumulative_return']==0
    assert return_statistics(div,basis='price_return')['cumulative_return']==pytest.approx(-.01)
    div[1]['DlyRet']='';assert return_statistics(div)['reason']=='missing_or_noncontiguous_returns'

def test_return_drawdown_zero_wealth_and_gaps():
    rows=[market_row('2020-01-01')]
    for i,r in enumerate([.1,-.2,.05],1):rows.append(market_row(str(dt.date(2020,1,1)+dt.timedelta(days=i)),DlyRet=str(r)))
    result=return_statistics(rows)
    assert result['maximum_drawdown']==pytest.approx(-.2)
    assert result['cumulative_return']==pytest.approx(1.1*.8*1.05-1)
    rows[-1]['DlyRet']='-1';assert return_statistics(rows)['maximum_drawdown']==-1
    rows[-1]['DlyPrevDt']='2010-01-01';assert return_statistics(rows)['reason']

def test_exports_and_rest_history_auth_uploads_jobs(market,tmp_path):
    root,source,_=market;s=MarketService(root)
    exported=s.export(securities=['10026'],format='parquet');assert Path(exported['path']).read_bytes()[:4]==b'PAR1'
    csv_result=s.export(securities=['10026'],format='csv');assert len(list(csv.DictReader(Path(csv_result['path']).open())))==2
    with TestClient(create_app(root,token='test-secret')) as c:
        assert c.get('/ui').status_code==200
        assert c.get('/v1/admin/history').status_code==401
        headers={'Authorization':'Bearer test-secret'}
        request={'security':'10026','as_of':'2020-12-31','mode':'research'}
        assert c.post('/v1/market/metrics',json=request,headers=headers).json()==s.daily_metrics(**request)
        upload_bytes=source.read_bytes();u=c.post('/v1/admin/uploads',json={'name':'data.csv','size':len(upload_bytes)},headers=headers).json()
        assert c.put(f"/v1/admin/uploads/{u['id']}/chunks?offset=1",content=b'x',headers=headers).status_code==400
        half=len(upload_bytes)//2
        assert c.put(f"/v1/admin/uploads/{u['id']}/chunks?offset=0",content=upload_bytes[:half],headers=headers).status_code==200
        assert c.get(f"/v1/admin/uploads/{u['id']}",headers=headers).json()['offset']==half
        assert c.put(f"/v1/admin/uploads/{u['id']}/chunks?offset={half}",content=upload_bytes[half:],headers=headers).status_code==200
        assert c.post(f"/v1/admin/uploads/{u['id']}/finish",json={'sha256':'wrong'},headers=headers).status_code==400
        assert c.post(f"/v1/admin/uploads/{u['id']}/finish",json={},headers=headers).json()['status']=='ready'
        assert c.post('/v1/admin/uploads',json={'name':'../outside.csv','size':1},headers=headers).status_code==400
        assert c.post('/v1/admin/imports',json={'upload_id':u['id'],'scope':'full'},headers=headers).status_code==400
        job=c.post('/v1/admin/imports',json={'upload_id':u['id'],'scope':'sample'},headers=headers).json()
        for _ in range(30):
            jobs=c.get('/v1/admin/imports',headers=headers).json()['rows']
            if jobs[0]['status'] in {'complete','failed'}:break
            time.sleep(.1)
        assert jobs[0]['status']=='complete' # bounded uploaded CSV is a user-selected sample
        assert s.status()['scope']=='mixed'
        hist=c.get('/v1/admin/history',headers=headers).json()
        assert any('metrics' in r['name'] for r in hist['rows'])
        assert 'test-secret' not in json.dumps(hist)


def test_daily_series_technical_export_versions_and_no_mutated_security_metadata(market):
    root,_,_=market;s=MarketService(root)
    r=s.metrics_series('10026',start='2020-12-31',end='2020-12-31',mode='research')
    assert len(r['results'])==1 and r['results'][0]['metrics']['pe_ttm']['value']==6.25
    for dataset in ['technical','daily_metrics']:
        result=s.export(securities=['10026'],start='2020-12-31',end='2020-12-31',dataset=dataset,format='parquet')
        assert result['metadata']['securities']==['10026']
        assert Path(result['path']).read_bytes()[:4]==b'PAR1'
        import duckdb
        with duckdb.connect() as d:
            rows=d.execute('DESCRIBE SELECT * FROM read_parquet(?)',[result['path']]).fetchall()
            names={r[0] for r in rows}
            assert ('adjusted_price' if dataset=='technical' else 'pe_ttm') in names
            assert 'DlyPrc' not in names
    with TestClient(create_app(root)) as c:
        request=dict(security='10026',start='2020-12-31',end='2020-12-31',mode='research')
        assert c.post('/v1/market/metrics',json=request).json()==s.metrics_series(**request)
        rows=c.get('/v1/admin/history').json()['rows']
        generation=next(r['generation'] for r in rows if '/metrics' in r['name'])
        assert s.status()['market_generation'] in generation
        assert 'financial_generation' in generation


def test_audited_identity_link_pins_versions_and_preserves_unit_gate(market,tmp_path):
    from fundamentals.market_links import import_links
    root,_,old=market
    source=tmp_path/'different.csv';csv_market(source,[market_row(PERMCO='00002',CUSIP='88888888',CUSIP9='888888888',HdrCUSIP='88888888',HdrCUSIP9='888888888')]);before=import_market(source,root,scope='full')
    s=MarketService(root);assert s.daily_metrics('10026','2020-12-31',mode='research')['matching_status']=='unmatched'
    links=tmp_path/'links.csv';links.write_text('gvkey,permno,valid_from,valid_to,evidence\n001690,10026,2020-01-01,2021-12-31,Synthetic identity audit\n')
    linked=import_links(links,root)
    result=s.daily_metrics('10026','2020-12-31',mode='research')
    assert result['matching_status']=='manual_identity_price_supported'
    assert result['metrics']['pe_ttm']['value']==6.25
    assert s.daily_metrics('10026','2020-12-31',mode='research',generation=before['generation'])['matching_status']=='unmatched'
    assert import_links(links,root)['already_imported']
    csv_market(source,[market_row(PERMCO='00002',CUSIP='88888888',CUSIP9='888888888',HdrCUSIP='88888888',HdrCUSIP9='888888888',DlyPrc='1')]);import_market(source,root,scope='full')
    assert s.daily_metrics('10026','2020-12-31',mode='research')['metrics']['pe_ttm']['reason']=='financial_price_share_basis_mismatch'


def test_malformed_numeric_and_local_admin_origin_boundaries(market,tmp_path):
    root,_,_=market
    source=tmp_path/'bad_numbers.csv'
    csv_market(source,[market_row('2020-09-30',DlyPrc='bad'),market_row(DlyPrc='bad')])
    import_market(source,root,scope='full')
    s=MarketService(root)
    row=s.prices('10026')['rows'][-1]
    assert row['DlyPrc'] is None and 'DlyPrc' in row['_invalid_numeric_fields']
    assert s.daily_metrics('10026','2020-12-31',mode='research')['metrics']['pe_ttm']['value'] is None
    with TestClient(create_app(root)) as c:
        assert c.post('/v1/admin/uploads',json={'name':'valid.csv','size':1},headers={'Origin':'http://evil.invalid'}).status_code==403
        assert c.get('/v1/status',headers={'Host':'evil.invalid'}).status_code==400


def test_progress_failure_does_not_publish_broken_generation(market,tmp_path):
    root,_,old=market;source=tmp_path/'next.csv';csv_market(source,[market_row(DlyPrc='99')])
    def callback(progress):
        if progress.get('complete'):raise RuntimeError('progress_sink_failed')
    with pytest.raises(RuntimeError):import_market(source,root,scope='full',progress=callback)
    assert MarketStore(root).manifest()['generation']==old['generation']
    assert float(MarketService(root).prices('10026')['rows'][-1]['DlyPrc'])==50


def dividend_split_rows():
    return [market_row('2021-01-01',DlyPrc='100',DlyOpen='100',DlyHigh='101',DlyLow='99',DlyClose='100'),
            market_row('2021-01-02',DlyPrc='99',DlyOpen='99',DlyHigh='100',DlyLow='98',DlyClose='99',DlyRet='0',DlyRetx='-0.01'),
            market_row('2021-01-03',DlyPrc='49.5',DlyOpen='49.5',DlyHigh='50',DlyLow='49',DlyClose='49.5',DlyRet='0',DlyRetx='0',DlyFacPrc='2')]


def test_adjustment_general_technical_and_explicit_defaults_dividend_split():
    from fundamentals.price_adjustment import adjusted_ohlc
    rows=dividend_split_rows()
    forward=adjusted_ohlc(rows)
    backward=adjusted_ohlc(rows,adjustment='backward')
    assert [r['adjusted_close'] for r in forward]==[49.5,49.5,49.5]
    assert [r['adjusted_close'] for r in backward]==[100,100,100]
    technical=adjusted_ohlc(rows,purpose='technical')
    assert [r['adjusted_close'] for r in technical]==[50,49.5,49.5]
    assert [r['adjusted_close'] for r in adjusted_ohlc(rows,purpose='technical',adjustment='backward')]==[100,99,99]
    assert adjusted_ohlc(rows,purpose='technical',basis='total_return')==[{**r,'purpose':'technical'} for r in forward]
    assert adjusted_ohlc(rows,basis='price_return')==[{**r,'purpose':'general'} for r in technical]
    assert forward[0]['adjusted_open']==49.5 and forward[0]['adjusted_high']==pytest.approx(49.995)
    assert [r['raw_volume'] for r in forward]==[100,100,100]
    assert {r['anchor_date'] for r in forward}=={'2021-01-03'}
    assert {r['anchor_date'] for r in backward}=={'2021-01-01'}


def test_adjustment_gaps_conflicts_and_bad_directions():
    from fundamentals.price_adjustment import adjusted_ohlc
    rows=dividend_split_rows();rows[1]['DlyRet']=''
    assert [r['anchor_date'] for r in adjusted_ohlc(rows)]==['2021-01-01','2021-01-03','2021-01-03']
    assert [r['anchor_date'] for r in adjusted_ohlc(rows,adjustment='backward')]==['2021-01-01','2021-01-02','2021-01-02']
    rows[1]['_quote_conflict']=True
    assert adjusted_ohlc(rows)[1]['adjusted_close'] is None
    assert adjusted_ohlc(rows,adjustment='backward')[1]['adjusted_close'] is None
    with pytest.raises(ValueError):adjusted_ohlc(rows,adjustment='unknown')
    with pytest.raises(ValueError):adjusted_ohlc(rows,purpose='unknown')
    with pytest.raises(ValueError):adjusted_ohlc(rows,basis='unknown')


def test_adjustment_rest_service_and_export_same_defaults_pagination(market,tmp_path):
    import duckdb
    root,_,_=market;source=tmp_path/'dividend_split.csv';csv_market(source,dividend_split_rows());import_market(source,root,scope='full')
    s=MarketService(root);request=dict(security='10026',start='2021-01-01',end='2021-01-03')
    assert s.technical(**request)['basis']=='price_return'
    assert s.technical(**request)['rows'][0]['adjusted_price']==50
    with TestClient(create_app(root)) as client:
        for direction in ['forward','backward']:
            for purpose,basis in [('general',None),('technical',None),('technical','total_return'),('general','price_return')]:
                params={**request,'adjustment':direction,'purpose':purpose,'basis':basis}
                expected=s.adjusted_prices(**params)
                response=client.post('/v1/market/adjusted-prices',json=params)
                assert response.status_code==200 and response.json()==expected
                assert expected['basis']==(basis or ('total_return' if purpose=='general' else 'price_return'))
                # Page 2 uses the anchor of the whole requested interval.
                assert s.adjusted_prices(**params,limit=1,offset=1)['rows']==expected['rows'][1:2]
                assert expected['expected_anchor_date']==('2021-01-03' if direction=='forward' else '2021-01-01')
                for fmt in ['csv','parquet']:
                    artifact=s.export(securities=['10026'],start=request['start'],end=request['end'],dataset='adjusted_prices',format=fmt,adjustment=direction,purpose=purpose,basis=basis)
                    assert artifact['metadata']['basis']==expected['basis']
                    with duckdb.connect() as c:
                        reader='read_csv_auto' if fmt=='csv' else 'read_parquet'
                        values=c.execute(f'SELECT adjusted_close,anchor_date,basis FROM {reader}(?) ORDER BY date',[artifact['path']]).fetchall()
                    assert [r[0] for r in values]==[r['adjusted_close'] for r in expected['rows']]
                    assert {str(r[1]) for r in values}=={r['anchor_date'] for r in expected['rows']}
                    assert {r[2] for r in values}=={expected['basis']}
        # Technical exports use technical defaults; explicit total_return overrides them.
        for explicit,first in [(None,50),('total_return',49.5)]:
            artifact=s.export(securities=['10026'],start=request['start'],end=request['end'],dataset='technical',basis=explicit,format='parquet')
            with duckdb.connect() as c:assert c.execute('SELECT adjusted_price FROM read_parquet(?) ORDER BY date LIMIT 1',[artifact['path']]).fetchone()[0]==first
            assert artifact['metadata']['basis']==(explicit or 'price_return')
