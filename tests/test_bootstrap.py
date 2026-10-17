import csv
import io
import json
import time
from fastapi.testclient import TestClient
from fundamentals.api import create_app
from fundamentals.importer import make_dictionary
from fundamentals.currency import value_fingerprint,matching_price_evidence,matching_share_evidence,share_evidence
from test_financial_service import financial_rows,HEADERS


def test_empty_database_is_usable_and_protected(tmp_path):
    with TestClient(create_app(tmp_path/'data',token='synthetic-test-token')) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/ui').status_code==200
        assert client.get('/v1/status').status_code==401
        headers={'Authorization':'Bearer synthetic-test-token'}
        for path in ['/v1/status','/v1/market/status','/v1/sec/status']:
            result=client.get(path,headers=headers);assert result.status_code==200
            assert result.json()['status']=='not_initialized'
        assert client.get('/v1/companies',headers=headers).json()['companies']==[]
        assert client.get('/v1/dictionary?limit=1000',headers=headers).json()['total']==679
        assert client.get('/v1/sec/mappings?limit=1000',headers=headers).json()['total']==679
        assert client.post('/v1/metrics',json={'company':'SYN'},headers=headers).status_code==400
        assert client.post('/v1/sec/sync',json={},headers=headers).status_code==400


def test_web_upload_from_empty_library_then_query_and_recreate(tmp_path,monkeypatch):
    monkeypatch.setenv('FINANCIAL_ALLOW_FULL_IMPORT','1')
    text=io.StringIO();writer=csv.DictWriter(text,HEADERS);writer.writeheader();writer.writerows(financial_rows());body=text.getvalue().encode()
    root=tmp_path/'data'
    with TestClient(create_app(root)) as client:
        upload=client.post('/v1/admin/uploads',json={'name':'synthetic.csv','size':len(body)}).json();uid=upload['id']
        assert client.put('/v1/admin/uploads/'+uid+'/chunks?offset=0',content=body).status_code==200
        assert client.post('/v1/admin/uploads/'+uid+'/finish',json={}).json()['preview']['kind']=='financial'
        job=client.post('/v1/admin/imports',json={'upload_id':uid,'kind':'financial','scope':'full'}).json()
        for _ in range(100):
            result=next(x for x in client.get('/v1/admin/imports').json()['rows'] if x['id']==job['job_id'])
            if result['status'] in {'complete','failed'}:break
            time.sleep(.05)
        assert result['status']=='complete',result
        first=client.post('/v1/metrics',json={'company':'AAPL','as_of':'2020-12-31','mode':'research'}).json()
        assert first['results'][0]['metrics']['pe_ttm']['value']==6.25
    with TestClient(create_app(root)) as client:
        second=client.post('/v1/metrics',json={'company':'AAPL','as_of':'2020-12-31','mode':'research'}).json()
        assert second==first
        assert client.get('/v1/status').json()['current_rows']==12


def test_bundled_schema_preserves_all_original_types():
    from importlib.resources import files
    schema=json.loads(files('fundamentals').joinpath('compustat_schema.json').read_text())
    inferred=make_dictionary(list(schema))
    assert len(inferred)==679
    assert all(inferred[k]['type']==v['type'] for k,v in schema.items())
    for key in ['gvkey','cik','cusip']:assert inferred[key]['type']=='VARCHAR'


def test_published_evidence_has_no_financial_values():
    from importlib.resources import files
    for name in ['price_currency_evidence.json','share_basis_evidence.json']:
        records=json.loads(files('fundamentals').joinpath(name).read_text())['records']
        assert all(not {'price','local_value','official_native_value','comparison_value','source_row','source_batch_id','adrrq'}.intersection(r) for r in records)
        assert not any(isinstance(v,(int,float)) for r in records for v in r.values())


def test_hash_evidence_matches_synthetic_values(monkeypatch):
    price={'gvkey':'999999','period':'2025-01-31','currency':'USD','price_hash':value_fingerprint(50)}
    monkeypatch.setattr('fundamentals.currency.price_evidence',lambda:({('999999','2025-01-31'):price},'synthetic'))
    row=dict(gvkey='999999',datadate='2025-01-31',curcdq='USD',prccq=50)
    assert matching_price_evidence(row)==price
    assert matching_price_evidence({**row,'prccq':51}) is None
    record=dict(gvkey='999999',datadate='2025-01-31',source_row_hash='synthetic',local_currency='USD',adr_ratio_hash=value_fingerprint(2),field='epsfiq',local_value_hash=value_fingerprint(3))
    monkeypatch.setattr('fundamentals.currency.share_evidence',lambda:({('999999','2025-01-31','synthetic'):[record]},'synthetic'))
    inputs={**row,'_row_hash':'synthetic','adrrq':2,'epsfiq':3}
    assert matching_share_evidence(inputs)==[record]
    assert not matching_share_evidence({**inputs,'epsfiq':4})
    assert not matching_share_evidence({**inputs,'adrrq':3})
