import csv
import datetime as dt
import json
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient
import pytest

from fundamentals.api import create_app,MetricQuery,ScreenQuery
from fundamentals.importer import import_file,METRIC_FIELDS,verify_securities
from fundamentals.metrics import calculate
from fundamentals.service import FinancialService
from fundamentals.store import Store

HEADERS=list(dict.fromkeys(METRIC_FIELDS+["datafmt","indfmt","consol","cusip","busdesc"]))

def write_csv(path,rows):
    with path.open("w",newline="") as f:
        writer=csv.DictWriter(f,HEADERS)
        writer.writeheader()
        writer.writerows(rows)

def financial_rows():
    rows=[]
    for year in (2019,2020,2021):
        for quarter,month,day in ((1,3,31),(2,6,30),(3,9,30),(4,12,31)):
            end=dt.date(year,month,day)
            rows.append(dict(gvkey="001690",datadate=str(end),conm="Test Apple",tic="AAPL",exchg="14",fic="USA",loc="USA",priusa="01",costat="A",curcdq="USD",fyearq=str(year),fqtr=str(quarter),fyr="12",datafmt="STD",indfmt="INDL",consol="C",rdq=str(end+dt.timedelta(days=45)),ajexq="1",prccq="50",cshoq="100",ceqq="400",seqq="500",atq="1000",ltq="500",niq="10",niy=str(quarter*10),saleq="100",saley=str(quarter*100),cogsq="60",oiadpq="20",epsfiq="2",epspiq="2.5",epsfxq="1.5",epspxq="2",epsfi12="8",epspi12="10",epsf12="6",epsx12="8",epsfiy=str(quarter*2),epspiy=str(quarter*2.5),epsfxy=str(quarter*1.5),epspxy=str(quarter*2),cusip="000001234",busdesc='A firm with "quoted" text, commas and\nnewlines'))
    return rows

def write_mapping(path,currency="USD"):
    columns=["gvkey","security_id","price_currency","valid_from","valid_to","evidence","split_basis"]
    with path.open("w",newline="") as f:
        writer=csv.DictWriter(f,columns)
        writer.writeheader()
        writer.writerow(dict(gvkey="001690",security_id="01",price_currency=currency,valid_from="1900-01-01",valid_to="2100-01-01",evidence="Audited synthetic fixture, not a real security verification",split_basis="unadjusted_prccq_ajexq"))

@pytest.fixture
def data(tmp_path):
    source=tmp_path/"source.csv"
    rows=financial_rows()
    write_csv(source,rows)
    root=tmp_path/"db"
    report=import_file(source,root,progress=lambda _:None)
    return root,source,rows,report

def test_preserves_identifiers_dates_strings_and_duplicate_provenance(tmp_path):
    rows=financial_rows()
    rows.append(rows[0].copy())
    rows[1]["saleq"]="not_a_number"
    source=tmp_path/"source.csv"
    write_csv(source,rows)
    report=import_file(source,tmp_path/"db",progress=lambda _:None)
    assert report["source_rows"]==13
    assert report["duplicate_rows"]==1
    assert report["reconciliation_ok"]
    assert report["invalid_value_counts"]["saleq"]==1
    service=FinancialService(tmp_path/"db")
    result=service.get_financials("001690",fields=["gvkey","cusip","saleq","busdesc"],mode="research")
    assert result["rows"][0]["gvkey"]=="001690"
    assert result["rows"][0]["cusip"]=="000001234"
    assert result["rows"][1]["saleq"] is None
    assert result["rows"][0]["busdesc"]==rows[0]["busdesc"]
    with Store(tmp_path/"db").connect() as con:
        assert len(con.execute("DESCRIBE financial_current").fetchall())==len(HEADERS)+5
        assert con.execute("SELECT gvkey,cusip,saleq FROM financial_versions WHERE _source_row=3").fetchone()==("001690","000001234",None)

def test_publication_lag_retrospective_and_stale_price(data):
    root,source,rows,report=data
    service=FinancialService(root)
    historical=service.get_metrics("AAPL",as_of="2020-12-31")["results"][0]
    research=service.get_metrics("AAPL",as_of="2020-12-31",mode="research")["results"][0]
    assert historical["financial_period"]=="2020-09-30"
    assert research["financial_period"]=="2020-12-31"
    assert historical["metrics"]["pe_ttm"]["value"]==6.25
    assert historical["price_currency_source"]=="inferred_curcdq"
    strict_currency=service.get_metrics("AAPL",as_of="2020-12-31",currency_policy="verified_only")["results"][0]
    assert strict_currency["metrics"]["pe_ttm"]["reason"]=="unverified_price_currency"
    stale=service.get_metrics("AAPL",as_of="2023-01-01")["results"][0]
    assert stale["valuation_status"]=="stale_price"
    assert historical["metrics"]["debt_ratio"]["value"]==0.5
    assert historical["metrics"]["roe"]["value"]==0.08

def test_security_mapping_pe_static_and_currency_guard(data,tmp_path):
    root,*_=data
    mapping=tmp_path/"mapping.csv"
    write_mapping(mapping)
    verify_securities(mapping,root)
    service=FinancialService(root)
    metrics=service.get_metrics("AAPL",as_of="2020-12-31",mode="research")["results"][0]["metrics"]
    assert metrics["pe_ttm"]["value"]==6.25
    assert metrics["pe_static"]["value"]==6.25
    assert metrics["pe_annualized"]["value"]==6.25
    assert metrics["pb"]["value"]==12.5
    assert metrics["ps_ttm"]["value"]==12.5
    write_mapping(mapping,"CAD")
    verify_securities(mapping,root)
    bad=service.get_metrics("AAPL",as_of="2020-12-31",mode="research")["results"][0]
    assert bad["metrics"]["pe_ttm"]["reason"]=="price_and_financial_currency_mismatch"

def test_estimated_dates_and_no_ytd_double_count(tmp_path):
    rows=financial_rows()
    for row in rows:
        row["rdq"]=""
    source=tmp_path/"source.csv"
    write_csv(source,rows)
    root=tmp_path/"db"
    import_file(source,root,progress=lambda _:None)
    service=FinancialService(root)
    result=service.get_metrics("AAPL",as_of="2021-04-29")["results"][0]
    assert result["financial_period"]=="2020-09-30"
    result=service.get_metrics("AAPL",as_of="2021-04-30")["results"][0]
    assert result["financial_period"]=="2020-12-31"
    assert result["release_date_source"]=="estimated_120_days"
    assert result["metrics"]["roe"]["numerator"]==40

def test_split_adjusted_four_quarter_fallback_and_missing_quarter(data,tmp_path):
    root,*_=data
    with Store(root).connect() as con:
        from fundamentals.service import rows_from
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    for row in rows:
        row["epsfi12"]=None
        if row["datadate"]<dt.date(2020,9,30):
            row["ajexq"]=2
            row["epsfiq"]=4
    mapping=dict(gvkey="001690",security_id="01",price_currency="USD",valid_from="1900-01-01",valid_to="2100-01-01",evidence="synthetic",split_basis="unadjusted_prccq_ajexq")
    result=calculate(rows,"2020-12-31","research",mappings=[mapping])
    assert result["metrics"]["pe_ttm"]["value"]==6.25
    assert result["metrics"]["pe_ttm"]["source"]=="four_contiguous_split_aligned_quarters"
    missing=[r for r in rows if r["datadate"]!=dt.date(2020,6,30)]
    result=calculate(missing,"2020-12-31","research",mappings=[mapping])
    assert result["metrics"]["pe_ttm"]["value"] is None

def test_versions_idempotence_merge_failure_and_generation_pinning(data,tmp_path):
    root,source,rows,first=data
    service=FinancialService(root)
    old_generation=service.get_data_status()["database_generation"]
    assert import_file(source,root,progress=lambda _:None)["already_imported"]
    update=tmp_path/"update.csv"
    changed=rows[-1].copy()
    changed["niq"]="99"
    changed["fyearq"]="2021.0"
    changed["fqtr"]="4.0"
    changed["fyr"]="12.0"
    write_csv(update,[changed])
    with Store(root).connect() as in_flight:
        second=import_file(update,root,progress=lambda _:None)
        assert in_flight.execute("SELECT niq FROM metric_inputs ORDER BY datadate DESC LIMIT 1").fetchone()[0]==10
    current=service.get_financials("AAPL",fields=["niq"],mode="research")["rows"]
    assert len(current)==12 and current[-1]["niq"]==99
    prior=service.get_financials("AAPL",fields=["niq"],batch_id=first["batch_id"])["rows"]
    assert prior[-1]["niq"]==10
    assert service.get_financials("AAPL",fields=["niq"],generation=old_generation)["rows"][-1]["niq"]==10
    active=Store(root).active_path()
    invalid=tmp_path/"invalid.csv"
    invalid.write_text("gvkey,datadate\n\"unterminated\n")
    with pytest.raises(Exception):
        import_file(invalid,root,progress=lambda _:None)
    assert Store(root).active_path()==active

def test_conflicting_fiscal_periods_not_collapsed(tmp_path):
    rows=financial_rows()
    conflict=rows[-1].copy()
    conflict["fyearq"]="2022"
    conflict["fqtr"]="1"
    conflict["fyr"]="9"
    rows.append(conflict)
    source=tmp_path/"source.csv"
    write_csv(source,rows)
    root=tmp_path/"db"
    report=import_file(source,root,progress=lambda _:None)
    assert report["retained_rows"]==13
    result=FinancialService(root).get_metrics("AAPL",as_of="2021-12-31",mode="research")["results"][0]
    assert result["metrics"]["roe"]["reason"]=="ambiguous_financial_period"

def test_rest_mcp_shared_results_filters_exports_auth_and_validation(data,tmp_path):
    root,*_=data
    app=create_app(root,token="test-secret")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code==200
        assert client.get("/v1/status").status_code==401
        headers={"Authorization":"Bearer test-secret"}
        payload={"company":"AAPL","as_of":"2020-12-31","mode":"research"}
        rest=client.post("/v1/metrics",headers=headers,json=payload)
        assert rest.status_code==200,rest.text
        assert rest.json()==app.state.service.get_metrics(**MetricQuery(**payload).model_dump())
        assert client.post("/v1/metrics",headers=headers,json={**payload,"mode":"strict"}).status_code==400
        assert client.post("/v1/metrics",headers=headers,json={"company":"AAPL","start":"2021-01-01","end":"2020-01-01"}).status_code==422
        assert client.get("/v1/metrics",headers=headers,params={"company":"AAPL","limit":1001}).status_code==422
        assert client.post("/v1/screen",headers=headers,json={"as_of":"2020-12-31","filters":[{"metric":"sql","op":"gt","value":0}]}).status_code==400
        screen=client.post("/v1/screen",headers=headers,json={"as_of":"2020-12-31","filters":[{"metric":"debt_ratio","op":"lte","value":0.6}]})
        assert screen.status_code==200,screen.text
        assert screen.json()["total"]==1
        exported=client.post("/v1/export",headers=headers,json={"as_of":"2020-12-31","format":"parquet"})
        assert exported.status_code==200,exported.text
        artifact=exported.json()
        assert client.get("/v1/exports/"+artifact["export_id"],headers=headers).content[:4]==b"PAR1"
        with duckdb.connect() as con:
            assert con.execute("SELECT count(*) FROM read_parquet(?)",[artifact["path"]]).fetchone()[0]==1
        assert client.get("/v1/exports/../../active.json",headers=headers).status_code!=200

def test_negative_profit_zero_denominator_and_basic_eps(data):
    root,*_=data
    from fundamentals.service import rows_from
    with Store(root).connect() as con:
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    mapping=dict(gvkey="001690",security_id="01",price_currency="USD",valid_from="1900-01-01",valid_to="2100-01-01",evidence="synthetic",split_basis="unadjusted_prccq_ajexq")
    basic=calculate(rows,"2020-12-31","research",eps_basis="basic",mappings=[mapping])
    assert basic["metrics"]["pe_ttm"]["value"]==5
    for row in rows:
        row["niq"]=-10
        row["epsfi12"]=-8
        row["atq"]=0
    result=calculate(rows,"2020-12-31","research",mappings=[mapping])
    assert result["metrics"]["pe_ttm"]["reason"]=="nonpositive_earnings_or_price"
    assert result["metrics"]["roe"]["value"]==-0.08
    assert result["metrics"]["debt_ratio"]["reason"]=="zero_denominator"

def test_ambiguous_ticker_requires_gvkey_and_rejects_overlapping_mappings(data,tmp_path):
    root,_,rows,_=data
    another=rows[-1].copy()
    another["gvkey"]="001691"
    source=tmp_path/"another.csv"
    write_csv(source,[another])
    import_file(source,root,progress=lambda _:None)
    with pytest.raises(ValueError,match="ambiguous_company_code"):
        FinancialService(root).get_metrics("AAPL",as_of="2021-12-31")
    FinancialService(root).get_metrics("001690",as_of="2021-12-31")
    path=tmp_path/"mapping.csv"
    write_mapping(path)
    with path.open() as stream:
        content=stream.read()
    path.write_text(content+content.splitlines()[1]+"\n")
    active=Store(root).active_path()
    with pytest.raises(ValueError,match="overlapping_or_duplicate"):
        verify_securities(path,root)
    assert Store(root).active_path()==active

def test_screen_preserves_older_visible_financials_when_recent_releases_are_delayed(tmp_path):
    rows=financial_rows()
    seed=rows[0]
    rows=[]
    for year in range(2010,2026):
        for quarter,month,day in ((1,3,31),(2,6,30),(3,9,30),(4,12,31)):
            row=seed.copy()
            row.update(datadate=f"{year}-{month:02}-{day}",fyearq=str(year),fqtr=str(quarter),rdq="2030-01-01" if year>=2018 else f"{year+1}-02-01")
            rows.append(row)
    source=tmp_path/"source.csv"
    write_csv(source,rows)
    root=tmp_path/"db"
    import_file(source,root,progress=lambda _:None)
    service=FinancialService(root)
    queried=service.get_metrics("001690",as_of="2025-12-31")["results"][0]
    screened=service.screen_companies("2025-12-31")["rows"][0]
    assert queried["financial_period"]==screened["financial_period"]=="2017-12-31"
    assert queried["metrics"]["debt_ratio"]["value"]==screened["debt_ratio"]


def test_default_currency_pe_pb_ps_screen_cache_and_export(data):
    root,*_=data
    service=FinancialService(root)
    point=service.get_metrics("AAPL",as_of="2020-12-31",mode="research")["results"][0]
    assert {k:point["metrics"][k]["value"] for k in ["pe_ttm","pe_static","pe_annualized","pb","ps_ttm"]}=={
        "pe_ttm":6.25,"pe_static":6.25,"pe_annualized":6.25,"pb":12.5,"ps_ttm":12.5}
    assert point["price_currency"]=="USD" and not point["price_currency_verified"]
    assert point["share_basis_status"]=="assumed_company_share_basis"
    query=dict(as_of="2020-12-31",mode="research",filters=[dict(metric="pe_ttm",op="lt",value=10)])
    assert service.screen_companies(**query)["total"]==1
    assert service.screen_companies(**query,currency_policy="verified_only")["total"]==0
    assert service.screen_companies(**query)["total"]==1
    content,meta=service.export_dataset(**query)
    row=list(csv.DictReader(content.decode("utf-8-sig").splitlines()))[0]
    assert row["price_currency_source"]=="inferred_curcdq"
    assert float(row["pe_ttm"])==6.25
    assert meta["currency_policy"]=="infer_curcdq"
    assert meta["valuation_policy_version"] and meta["price_currency_evidence_version"]


def test_exact_currency_evidence_is_not_a_historical_interval(data):
    root,*_=data
    from fundamentals.service import rows_from
    with Store(root).connect() as con:
        row=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate DESC LIMIT 1"))[0]
    row.update(datadate=dt.date(2025,12,31),prccq=271.86,fyearq=2025,rdq=dt.date(2026,1,29))
    result=calculate([row],"2025-12-31","research",currency_policy="verified_only")
    assert result["price_currency_source"]=="external_price_comparison"
    assert result["price_currency_verified"]
    assert result["metrics"]["pe_ttm"]["value"]==pytest.approx(271.86/8)
    assert result["price_currency_evidence"]["source_url"]
    for changed in [{"prccq":272},{"datadate":dt.date(2025,12,30)},{"curcdq":"CAD"}]:
        result=calculate([{**row,**changed}],"2025-12-31","research",currency_policy="verified_only")
        assert result["metrics"]["pe_ttm"]["reason"]=="unverified_price_currency"


@pytest.mark.parametrize("adr_ratio,primary",[(5,"90"),(1,"90"),(8,"01")])
def test_default_adr_units_are_not_converted_again(data,adr_ratio,primary):
    root,*_=data
    from fundamentals.service import rows_from
    with Store(root).connect() as con:
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    for row in rows:
        row.update(adrrq=adr_ratio,priusa=primary)
    result=calculate(rows,"2020-12-31","research")
    assert result["share_basis_status"]=="assumed_ads_equivalent"
    assert result["adr_ratio"]==adr_ratio and not result["adr_ratio_applied"]
    assert not result["share_basis_verified"]
    for name,expected in [("pe_ttm",6.25),("pe_static",6.25),("pe_annualized",6.25),("pb",12.5),("ps_ttm",12.5)]:
        assert result["metrics"][name]["value"]==expected
        strict=calculate(rows,"2020-12-31","research",share_policy="verified_only")
        assert strict["metrics"][name]["reason"]=="unverified_adr_share_basis"
    assert result["metrics"]["roe"]["value"]==0.08
    mapping=dict(gvkey="001690",security_id="audited_receipt",price_currency="USD",valid_from="1900-01-01",valid_to="2100-01-01",evidence="Audited synthetic per-receipt inputs",split_basis="unadjusted_prccq_ajexq")
    assert calculate(rows,"2020-12-31","research",mappings=[mapping])["metrics"]["pe_ttm"]["value"]==6.25


def test_inference_retains_currency_conflict_and_security_guards(data):
    root,*_=data
    from fundamentals.service import rows_from
    with Store(root).connect() as con:
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    rows[-1]["curcdq"]="CAD"
    # The last price is CAD, while the latest financial statement visible at
    # year-end is still USD. Equal currency is required across both dates.
    result=calculate(rows,"2021-12-31")
    assert result["metrics"]["pe_ttm"]["reason"]=="price_and_financial_currency_mismatch"
    rows[-1]["curcdq"]="USD"
    rows[-2]["adrrq"]=5
    assert calculate(rows,"2021-12-31")["metrics"]["pe_ttm"]["reason"]=="security_share_basis_changed_across_inputs"
    rows[-1].update(curcdq="USD",priusa=None,prican=None)
    assert calculate(rows,"2021-12-31","research")["metrics"]["pe_ttm"]["reason"]=="unverified_security_share_basis"


def test_rest_policy_schema_and_verified_only(data):
    root,*_=data
    with TestClient(create_app(root)) as client:
        payload=dict(company="AAPL",as_of="2020-12-31",mode="research")
        default=client.get("/v1/metrics",params=payload).json()
        assert default==client.post("/v1/metrics",json=payload).json()
        assert default["results"][0]["metrics"]["pe_ttm"]["value"]==6.25
        verified=client.post("/v1/metrics",json={**payload,"currency_policy":"verified_only"}).json()
        assert verified["results"][0]["metrics"]["pe_ttm"]["reason"]=="unverified_price_currency"
        assert client.post("/v1/metrics",json={**payload,"currency_policy":"invalid"}).status_code==422
        assert client.post("/v1/metrics",json={**payload,"share_policy":"invalid"}).status_code==422
        assert client.get("/v1/status").json()["default_share_policy"]=="infer_compustat"
        assert client.get("/v1/status").json()["price_currency_verified_records"]==35


@pytest.mark.parametrize("ratio",[None,0,-2,float("nan"),float("inf")])
def test_invalid_adr_ratios_remain_blocked(data,ratio):
    from fundamentals.service import rows_from
    with Store(data[0]).connect() as con:
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    for row in rows:
        row.update(priusa="90",adrrq=ratio)
    assert calculate(rows,"2020-12-31","research")["metrics"]["pe_ttm"]["reason"]=="missing_or_invalid_adr_ratio"


def test_adr_ratio_changes_and_four_quarter_fallback(data):
    from fundamentals.service import rows_from
    with Store(data[0]).connect() as con:
        rows=rows_from(con.execute("SELECT * FROM metric_inputs ORDER BY datadate"))
    for row in rows:
        row.update(priusa="90",adrrq=5)
    rows[-2]["adrrq"]=2
    result=calculate(rows,"2021-12-31")
    assert result["metrics"]["pe_ttm"]["reason"]=="adr_ratio_changed_across_inputs"
    rows[-1]["epsfi12"]=None
    result=calculate(rows,"2021-12-31","research")
    assert result["metrics"]["pe_ttm"]["reason"]=="adr_ratio_changed_across_inputs"
    for row in rows:
        row["adrrq"]=5
    assert calculate(rows,"2021-12-31","research")["metrics"]["pe_ttm"]["value"]==6.25


def test_adr_share_policy_isolated_in_cache_rest_and_exports(tmp_path):
    rows=financial_rows()
    for row in rows:
        row.update(priusa="90",adrrq="5")
    source=tmp_path/"adr.csv"
    write_csv(source,rows)
    root=tmp_path/"db"
    import_file(source,root,progress=lambda _:None)
    service=FinancialService(root)
    query=dict(as_of="2020-12-31",mode="research",filters=[dict(metric="pe_ttm",op="lt",value=10)])
    assert service.screen_companies(**query)["total"]==1
    assert service.screen_companies(**query,share_policy="verified_only")["total"]==0
    assert service.screen_companies(**query)["total"]==1
    content,metadata=service.export_dataset(**query)
    point=list(csv.DictReader(content.decode("utf-8-sig").splitlines()))[0]
    assert point["share_policy"]==metadata["share_policy"]=="infer_compustat"
    assert point["share_basis_status"]=="assumed_ads_equivalent"
    assert point["adr_ratio_applied"]=="False"
    assert float(point["pe_ttm"])==6.25
    with TestClient(create_app(root)) as client:
        request=dict(company="001690",as_of="2020-12-31",mode="research")
        assert client.post("/v1/metrics",json=request).json()==service.get_metrics(**request)
        strict=client.post("/v1/metrics",json={**request,"share_policy":"verified_only"}).json()
        assert strict["results"][0]["metrics"]["pe_ttm"]["reason"]=="unverified_adr_share_basis"


def test_adr_evidence_fingerprint_half_year_and_mixed_classes(monkeypatch,synthetic_share_evidence):
    from fundamentals.currency import share_evidence,matching_share_evidence
    monkeypatch.setattr("fundamentals.currency.share_evidence",lambda:synthetic_share_evidence)
    records=[r for group in synthetic_share_evidence[0].values() for r in group]
    def record_row(ticker,field):
        evidence=next(r for r in records if r["ticker"]==ticker and r["field"]==field)
        row=dict(gvkey=evidence["gvkey"],tic=ticker,datadate=dt.date.fromisoformat(evidence["datadate"]),
                 _row_hash=evidence["source_row_hash"],_batch_id=evidence["source_batch_id"],_source_row=evidence["source_row"],
                 curcdq=evidence["local_currency"],adrrq=evidence["adrrq"],priusa="90",fyearq=2024,fqtr=4,fyr=12,
                 prccq=50,ajexq=1,epsfi12=8,epsfiy=8,cshoq=100,ceqq=400)
        row[field]=evidence["local_value"]
        return row
    bhp=record_row("BHP","epsfiq")
    result=calculate([bhp],str(bhp["datadate"]),"research")
    assert result["metrics"]["pe_ttm"]["value"]==6.25
    assert result["metrics"]["pe_annualized"]["reason"]=="nonquarter_eps_cannot_annualize_as_quarter"
    for change in [dict(_row_hash="different"),dict(datadate=dt.date(2024,12,30)),dict(adrrq=3),dict(curcdq="CAD")]:
        assert not matching_share_evidence({**bhp,**change})
    assert not any(r["field"]=="epsfiq" for r in matching_share_evidence({**bhp,"epsfiq":123}))
    pbr=record_row("PBR","cshoq")
    result=calculate([pbr],str(pbr["datadate"]),"research")
    assert result["metrics"]["pe_ttm"]["value"]==6.25
    for name in ["pb","ps_ttm"]:
        assert result["metrics"][name]["reason"]=="mixed_share_classes_not_matched"
    assert result["estimated_market_cap_millions"] is None


def test_known_half_year_blocks_fallback_and_all_eps_choices(monkeypatch,synthetic_share_evidence):
    from fundamentals.currency import share_evidence
    monkeypatch.setattr("fundamentals.currency.share_evidence",lambda:synthetic_share_evidence)
    record=next(r for group in synthetic_share_evidence[0].values() for r in group
                if r['ticker']=='BHP' and r['field']=='epsfiq')
    rows=[]
    for quarter,month,day in [(1,3,31),(2,6,30),(3,9,30),(4,12,31)]:
        rows.append(dict(gvkey=record['gvkey'],datadate=dt.date(2024,month,day),fyearq=2024,fqtr=quarter,fyr=12,
                         curcdq='USD',priusa='90',adrrq=2,prccq=50,ajexq=1,epsfiq=1,epspiq=1,epsfxq=1,epspxq=1,
                         _row_hash=str(quarter),_batch_id=record['source_batch_id'],_source_row=quarter))
    rows[-1].update(_row_hash=record['source_row_hash'],epsfiq=record['local_value'],_source_row=record['source_row'])
    for basis,extraordinary in [('diluted',True),('basic',True),('diluted',False),('basic',False)]:
        result=calculate(rows,'2024-12-31','research',eps_basis=basis,include_extraordinary=extraordinary)
        for name in ['pe_ttm','pe_static']:
            assert result['metrics'][name]['reason']=='nonquarter_eps_cannot_sum_four_quarters'
        assert result['metrics']['pe_annualized']['reason']=='nonquarter_eps_cannot_annualize_as_quarter'
    rows[-1]['epsfi12']=4
    rows[-1]['epsfiy']=4
    result=calculate(rows,'2024-12-31','research')
    assert result['metrics']['pe_ttm']['value']==12.5
    assert result['metrics']['pe_static']['value']==12.5
