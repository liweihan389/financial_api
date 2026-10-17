import datetime as dt
import math

from .currency import (CURRENCY_POLICIES, SHARE_POLICIES, evidence_status,
    matching_price_evidence, matching_share_evidence, share_inputs_reason,
    is_adr, nonquarter_eps, mixed_share_classes)

METRICS = {"pe_ttm", "pe_static", "pe_annualized", "pb", "ps_ttm", "roe", "roa", "gross_margin", "operating_margin", "net_margin", "revenue_yoy", "net_income_yoy", "revenue_ttm_yoy", "net_income_ttm_yoy", "debt_ratio"}
EPS_FIELDS = {
    ("diluted", True): ("epsfiq", "epsfi12", "epsfiy"),
    ("basic", True): ("epspiq", "epspi12", "epspiy"),
    ("diluted", False): ("epsfxq", "epsf12", "epsfxy"),
    ("basic", False): ("epspxq", "epsx12", "epspxy"),
}

def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None

def date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(value)

def availability(row, quarterly_lag=90, annual_lag=120):
    if row.get('_available_date'):
        return date(row['_available_date']),row.get('_release_source','sec_acceptance_time')
    dates = [(date(row[k]), k) for k in ("rdq", "fdateq") if row.get(k)]
    if dates:
        value, source = max(dates)
        return value, source
    end = date(row.get("apdedateq") or row["datadate"])
    lag = annual_lag if number(row.get("fqtr")) == 4 else quarterly_lag
    return end + dt.timedelta(days=lag), f"estimated_{lag}_days"

def quarter_id(row):
    year, quarter = number(row.get("fyearq")), number(row.get("fqtr"))
    if year is None or quarter not in {1,2,3,4} or year != int(year):
        return None
    return int(year) * 4 + int(quarter) - 1

def clean(row):
    return row.get("_conflict",1) == 1 and row.get("_period_conflict",1) == 1

def compatible(a,b):
    return clean(a) and clean(b) and a.get("curcdq") is not None and a.get("curcdq")==b.get("curcdq") and a.get("fyr")==b.get("fyr")

def window(rows, endpoint, offset=0):
    end_id = quarter_id(endpoint)
    if end_id is None:
        return []
    lookup = {}
    for row in rows:
        if compatible(endpoint,row):
            lookup.setdefault(quarter_id(row),[]).append(row)
    selected=[]
    for q in range(end_id-offset-3,end_id-offset+1):
        candidates=lookup.get(q,[])
        if len(candidates)!=1:
            return []
        selected.append(candidates[0])
    if any(not 60 <= (date(b["datadate"])-date(a["datadate"])).days <= 120 for a,b in zip(selected,selected[1:])):
        return []
    return selected

def aggregate(rows, field):
    values=[number(r.get(field)) for r in rows]
    return sum(values) if len(values)==4 and all(v is not None for v in values) else None

def item(formula, fields, numerator=None, denominator=None, reason=None, source=None, positive=False):
    if reason is None:
        if numerator is None or denominator is None:
            reason="missing_or_incomplete_inputs"
        elif denominator == 0:
            reason="zero_denominator"
        elif positive and (denominator < 0 or numerator <= 0):
            reason="nonpositive_earnings_or_price"
    value = numerator/denominator if reason is None else None
    if value is not None and not math.isfinite(value):
        value,reason=None,"nonfinite_result"
    return {"value":value,"formula":formula,"input_fields":fields,"numerator":numerator,"denominator":denominator,"source":source,"reason":reason}

def prior_year(rows, endpoint):
    q=quarter_id(endpoint)
    selected=[r for r in rows if q is not None and quarter_id(r)==q-4 and compatible(endpoint,r)]
    if len(selected)!=1:
        return None
    return selected[0] if 300 <= (date(endpoint["datadate"])-date(selected[0]["datadate"])).days <= 430 else None

def calculate(rows, as_of, mode="historical_approx", eps_basis="diluted", include_extraordinary=True, quarterly_lag=90, annual_lag=120, max_price_age=120, mappings=(), currency_policy="infer_curcdq", share_policy="infer_compustat", external_price=None):
    as_of=date(as_of)
    if mode not in {"research","historical_approx"}:
        raise ValueError("strict_point_in_time_not_supported" if mode=="strict" else "invalid_time_mode")
    if (eps_basis,include_extraordinary) not in EPS_FIELDS:
        raise ValueError("invalid_eps_basis")
    if currency_policy not in CURRENCY_POLICIES:
        raise ValueError("invalid_currency_policy")
    if share_policy not in SHARE_POLICIES:
        raise ValueError("invalid_share_policy")
    rows=sorted([r for r in rows if date(r["datadate"])<=as_of],key=lambda r:date(r["datadate"]))
    visible=[r for r in rows if not r.get('_price_only') and (mode=="research" or availability(r,quarterly_lag,annual_lag)[0]<=as_of)]
    latest=visible[-1] if visible else None
    price_rows=[r for r in rows if number(r.get("prccq")) is not None]
    price=price_rows[-1] if price_rows else None
    if external_price is not None:
        if date(external_price["datadate"])>as_of:raise ValueError("future_external_price")
        price=external_price if number(external_price.get("prccq")) is not None else None
    identity=rows[-1] if rows else {}
    release,release_source=availability(latest,quarterly_lag,annual_lag) if latest else (None,None)
    price_age=(as_of-date(price["datadate"])).days if price else None
    warnings=["revised_snapshot_not_point_in_time","company_alias_and_market_metadata_may_be_current"]
    if mode=="research":
        warnings.append("retrospective_financial_values_not_safe_for_as_of_backtests")
    if release_source and release_source.startswith("estimated"):
        warnings.append("estimated_release_date")
    output={"gvkey":identity.get("gvkey"),"ticker":identity.get("tic"),"company":identity.get("conm"),"as_of":as_of.isoformat(),"mode":mode,"eps_basis":eps_basis,"include_extraordinary":include_extraordinary,"financial_period":str(latest["datadate"]) if latest else None,"financial_currency":latest.get("curcdq") if latest else None,"release_date":str(release) if release else None,"release_date_source":release_source,"price_date":str(price["datadate"]) if price else None,"price":number(price.get("prccq")) if price else None,"price_age_days":price_age,"batch_id":latest.get("_batch_id") if latest else None,"warnings":warnings,"metrics":{},"input_records":[]}
    financial_reason="no_visible_financials" if latest is None else "ambiguous_financial_period" if not clean(latest) else None
    valuation_reason=financial_reason
    if valuation_reason is None:
        if price is None:
            valuation_reason="no_prior_price"
        elif not clean(price):
            valuation_reason="ambiguous_price_period"
        elif price_age>max_price_age:
            valuation_reason="stale_price"
        elif number(price.get("prccq"))<=0:
            valuation_reason="nonpositive_price"
    matching=[m for m in mappings if price and m["gvkey"]==identity.get("gvkey") and date(m["valid_from"])<=date(price["datadate"])<=date(m["valid_to"])]
    mapping=matching[0] if len(matching)==1 else None
    evidence=matching_price_evidence(price)
    price_currency=mapping["price_currency"] if mapping else evidence["currency"] if evidence else price.get("curcdq") if price and currency_policy=="infer_curcdq" else None
    currency_source="audited_security_mapping" if mapping else "external_price_comparison" if evidence else "inferred_curcdq" if price_currency else "unverified"
    def shares_reason(required):
        sec=[r for r in required if r and r.get('_source')=='sec']
        regular=[r for r in required if r and r.get('_source')!='sec']
        if any(r.get('_sec_known_share_conflict') for r in sec):return 'known_sec_share_class_conflict'
        if any(not r.get('_sec_share_scale_verified') for r in sec) and (mode!='research' or share_policy=='verified_only'):return 'sec_share_and_split_scale_unverified'
        return share_inputs_reason(regular,share_policy) if regular else None
    share_reason=shares_reason([price]+([latest] if latest else [])) if price else "no_prior_price"
    if valuation_reason is None:
        if len(matching)>1:
            valuation_reason="ambiguous_security_mapping"
        elif price_currency is None:
            valuation_reason="unverified_price_currency" if currency_policy=="verified_only" else "missing_price_currency"
        elif price_currency not in {"USD","CAD"}:
            valuation_reason="unsupported_price_currency"
        elif share_reason:
            valuation_reason=share_reason
    if valuation_reason is None and price_currency!=latest.get("curcdq"):
        valuation_reason="price_and_financial_currency_mismatch"
    if currency_source=="inferred_curcdq":
        warnings.append("price_currency_inferred_from_curcdq_not_independently_verified")
    if not mapping and price:
        warnings.append("adr_share_basis_requires_audit" if share_reason=="unverified_adr_share_basis" else "security_share_basis_requires_audit" if share_reason else "adr_eps_and_shares_assumed_already_ads_equivalent_no_ratio_conversion" if is_adr(price) else "company_share_basis_and_unadjusted_prccq_ajexq_assumed")
    output.update(evidence_status())
    output.update({"currency_policy":currency_policy,"price_currency":price_currency,
        "price_currency_source":currency_source,"price_currency_verified":currency_source in {"audited_security_mapping","external_price_comparison"},
        "price_currency_evidence":evidence if not mapping else None,
        "price_market":evidence["market"] if evidence and not mapping else None,
        "price_trading_date":evidence["trading_date"] if evidence and not mapping else None,
        "share_policy":share_policy,"share_basis_verified":bool(mapping),
        "adr_ratio":number(price.get("adrrq")) if price else None,"adr_ratio_applied":False,
        "share_basis_evidence":[],
        "share_basis_status":"audited_security_mapping" if mapping else share_reason or ("assumed_ads_equivalent" if is_adr(price) else "assumed_company_share_basis")})
    output["valuation_status"]=valuation_reason or ("verified_mapping" if mapping else "verified_currency_assumed_share_basis" if evidence else "inferred_currency_and_share_basis")
    output["security_mapping"]=mapping
    if not latest:
        for name in sorted(METRICS):
            output["metrics"][name]=item(name,[],reason=financial_reason)
        return output
    qeps,ttmeps,yeps=EPS_FIELDS[(eps_basis,include_extraordinary)]
    quarters=window(visible,latest)
    previous=prior_year(visible,latest)
    old_quarters=window(visible,latest,4)
    annuals=[r for r in visible if number(r.get("fqtr"))==4 and compatible(latest,r)]
    annual=annuals[-1] if annuals else None
    used={r["_row_hash"]:r for r in [latest]+quarters+old_quarters+([previous] if previous else [])+([annual] if annual else [])+([price] if price else [])}
    for row in used.values():
        av,source=availability(row,quarterly_lag,annual_lag)
        output["input_records"].append({"period":str(row["datadate"]),"fiscal_year":row.get("fyearq"),"fiscal_quarter":row.get("fqtr"),"release_date":str(av),"release_source":source,"batch_id":row["_batch_id"],"source_row":row["_source_row"]})
    price_factor=number(price.get("ajexq")) if price else None
    def adjust_eps(value,row):
        if row.get('_source')=='sec':
            if row.get('_sec_share_scale_verified'):
                factor=number(row.get('_sec_adjustment_factor'))
                return value/factor*price_factor if value is not None and factor and price_factor else None
            if mode!='research' or share_policy=='verified_only' or row.get('_sec_known_share_conflict'):return None
            if 'sec_as_reported_eps_and_shares_scale_assumed_unverified' not in warnings:warnings.append('sec_as_reported_eps_and_shares_scale_assumed_unverified')
            return value
        factor=number(row.get("ajexq"))
        if value is None or factor is None or factor<=0 or price_factor is None or price_factor<=0:
            return None
        return value/factor*price_factor
    def mapping_reason(required):
        if valuation_reason:
            return valuation_reason
        if mapping and any(not date(mapping["valid_from"])<=date(r["datadate"])<=date(mapping["valid_to"]) for r in required):
            return "security_mapping_does_not_cover_input_periods"
        if any(r.get("curcdq")!=price_currency for r in required):
            return "price_and_financial_currency_mismatch"
        if not mapping:
            return shares_reason([price]+required)
        return None
    diluted_ttm=number(latest.get(ttmeps))
    ttm_source=ttmeps
    ttm_required=[latest]
    ttm_period_reason=None
    if diluted_ttm is not None:
        diluted_ttm=adjust_eps(diluted_ttm,latest)
    elif quarters:
        if any(nonquarter_eps(r,qeps) for r in quarters):
            ttm_period_reason="nonquarter_eps_cannot_sum_four_quarters"
        adjusted=[adjust_eps(number(r.get(qeps)),r) for r in quarters]
        diluted_ttm=sum(adjusted) if all(v is not None for v in adjusted) else None
        ttm_source="four_contiguous_split_aligned_quarters"
        ttm_required=quarters
    static_eps=None
    annual_period_reason=None
    annual_required=[annual] if annual else []
    if annual:
        static_eps=adjust_eps(number(annual.get(yeps)),annual)
        if static_eps is None:
            annual_window=window(visible,annual)
            if any(nonquarter_eps(r,qeps) for r in annual_window):
                annual_period_reason="nonquarter_eps_cannot_sum_four_quarters"
            adjusted=[adjust_eps(number(r.get(qeps)),r) for r in annual_window]
            if len(adjusted)==4 and all(v is not None for v in adjusted):
                static_eps=sum(adjusted)
                ttm_annual_source="four_quarter_complete_fiscal_year"
                annual_required=annual_window
                used.update({r["_row_hash"]:r for r in annual_window})
                for row in annual_window:
                    if not any(r["source_row"]==row["_source_row"] and r["batch_id"]==row["_batch_id"] for r in output["input_records"]):
                        av,src=availability(row,quarterly_lag,annual_lag)
                        output["input_records"].append({"period":str(row["datadate"]),"release_date":str(av),"release_source":src,"batch_id":row["_batch_id"],"source_row":row["_source_row"]})
            else:
                ttm_annual_source=yeps
        else:
            ttm_annual_source=yeps
    else:
        ttm_annual_source=yeps
    p=number(price.get("prccq")) if price else None
    m=output["metrics"]
    m["pe_ttm"]=item("price / split_aligned_ttm_eps",["prccq",ttmeps,qeps,"ajexq"],p,diluted_ttm,mapping_reason(ttm_required) or ttm_period_reason,ttm_source,True)
    m["pe_static"]=item("price / latest_complete_fiscal_year_eps",["prccq",yeps,"ajexq"],p,static_eps,mapping_reason(annual_required) or annual_period_reason,ttm_annual_source,True)
    annualized=adjust_eps(number(latest.get(qeps)),latest)
    nonquarter_reason="nonquarter_eps_cannot_annualize_as_quarter" if nonquarter_eps(latest,qeps) else None
    m["pe_annualized"]=item("price / (latest_quarter_eps * 4)",["prccq",qeps,"ajexq"],p,None if nonquarter_reason else annualized*4 if annualized is not None else None,mapping_reason([latest]) or nonquarter_reason,"mechanical_annualization_not_forecast",True)
    revenue=aggregate(quarters,"saleq")
    net=aggregate(quarters,"niq")
    shares=number(latest.get("cshoq"))
    factor=number(latest.get("ajexq"))
    aligned_shares=shares*factor/price_factor if shares is not None and factor and price_factor and factor>0 and price_factor>0 else None
    if latest.get('_source')=='sec' and latest.get('_sec_share_scale_verified'):
        factor=number(latest.get('_sec_adjustment_factor'))
        aligned_shares=shares*factor/price_factor if shares is not None and factor and price_factor else None
    elif latest.get('_source')=='sec' and mode=='research' and share_policy!='verified_only' and not latest.get('_sec_known_share_conflict'):
        aligned_shares=shares
        if shares is not None:warnings.append('sec_period_end_shares_scale_assumed_unverified')
    marketcap=p*aligned_shares if p is not None and aligned_shares is not None and aligned_shares>0 else None
    classes_reason="mixed_share_classes_not_matched" if mixed_share_classes(latest) else None
    if classes_reason:
        warnings.append(classes_reason)
        marketcap=None
    output["estimated_market_cap_millions"]=marketcap if mapping_reason([latest]) is None else None
    m["pb"]=item("price * split_aligned_shares / common_equity",["prccq","cshoq","ajexq","ceqq"],marketcap,number(latest.get("ceqq")),mapping_reason([latest]) or classes_reason,"estimated_market_cap_using_last_visible_shares")
    m["ps_ttm"]=item("price * split_aligned_shares / ttm_sales",["prccq","cshoq","ajexq","saleq"],marketcap,revenue,mapping_reason(quarters+[latest]) or classes_reason,"estimated_market_cap_using_last_visible_shares")
    def avg(field):
        a,b=number(latest.get(field)),number(previous.get(field)) if previous else None
        return (a+b)/2 if a is not None and b is not None else None
    m["roe"]=item("ttm_net_income / average_total_stockholders_equity",["niq","seqq"],net,avg("seqq"),financial_reason,"four_quarter_net_income")
    m["roa"]=item("ttm_net_income / average_total_assets",["niq","atq"],net,avg("atq"),financial_reason,"four_quarter_net_income")
    sales,cogs,operating,profit=(number(latest.get(f)) for f in ("saleq","cogsq","oiadpq","niq"))
    m["gross_margin"]=item("(quarter_sales-quarter_cogs)/quarter_sales",["saleq","cogsq"],sales-cogs if sales is not None and cogs is not None else None,sales,financial_reason,"latest_visible_quarter")
    m["operating_margin"]=item("quarter_operating_income/quarter_sales",["oiadpq","saleq"],operating,sales,financial_reason,"latest_visible_quarter")
    m["net_margin"]=item("quarter_net_income/quarter_sales",["niq","saleq"],profit,sales,financial_reason,"latest_visible_quarter")
    m["debt_ratio"]=item("total_liabilities / total_assets",["ltq","atq"],number(latest.get("ltq")),number(latest.get("atq")),financial_reason,"latest_visible_quarter")
    for name,field,current_value,prior_value in [("revenue_yoy","saleq",sales,number(previous.get("saleq")) if previous else None),("net_income_yoy","niq",profit,number(previous.get("niq")) if previous else None),("revenue_ttm_yoy","saleq",revenue,aggregate(old_quarters,"saleq")),("net_income_ttm_yoy","niq",net,aggregate(old_quarters,"niq"))]:
        reason=financial_reason or ("nonpositive_comparison_base" if prior_value is not None and prior_value<=0 else None)
        m[name]=item("(current-prior_year)/prior_year",[field],current_value-prior_value if current_value is not None and prior_value is not None else None,prior_value,reason,"same_fiscal_quarter_prior_year" if "ttm" not in name else "four_quarter_windows")
    for row in used.values():
        for record in matching_share_evidence(row):
            output["share_basis_evidence"].append(record)
            if record["numeric_status"]=="unresolved_difference" and "issuer_numeric_difference_unresolved" not in warnings:
                warnings.append("issuer_numeric_difference_unresolved")
            if record["period_type"]=="half_year_EPS" and "source_eps_is_half_year_not_quarter" not in warnings:
                warnings.append("source_eps_is_half_year_not_quarter")
    sources={r.get('_source','compustat') for r in used.values()}
    output['data_sources']=sorted(sources)
    if len(sources)>1:warnings.append('mixed_compustat_sec_inputs_share_and_accounting_comparability_limited')
    required_by_metric={'pe_ttm':[price]+ttm_required,'pe_static':[price]+annual_required,'pe_annualized':[price,latest],'pb':[price,latest],'ps_ttm':[price,latest]+quarters,'roe':[latest,previous]+quarters,'roa':[latest,previous]+quarters,'gross_margin':[latest],'operating_margin':[latest],'net_margin':[latest],'debt_ratio':[latest],'revenue_yoy':[latest,previous],'net_income_yoy':[latest,previous],'revenue_ttm_yoy':quarters+old_quarters,'net_income_ttm_yoy':quarters+old_quarters}
    for name,metric in output['metrics'].items():
        actual={r['_row_hash']:r for r in required_by_metric[name] if r is not None}
        metric['input_facts']=[{'data_source':r.get('_source','compustat'),'period':str(r['datadate']),'batch_id':r.get('_batch_id'),'source_row':r.get('_source_row'),'source_row_hash':r.get('_row_hash'),'fields':{f:({'value':r.get(f),**r.get('_field_provenance',{}).get(f,{})}) for f in metric['input_fields'] if r.get(f) is not None}} for r in actual.values() if any(r.get(f) is not None for f in metric['input_fields'])]
        metric['quality']='inferred' if any(v.get('quality')=='inferred' for rec in metric['input_facts'] for v in rec['fields'].values()) else 'trusted_inputs'
        if name in {'pe_ttm','pe_static','pe_annualized','pb','ps_ttm'} and any(r.get('_source')=='sec' and not r.get('_sec_share_scale_verified') for r in actual.values()):
            metric['quality']='inferred'
            metric['quality_reasons']=['sec_share_and_split_scale_unverified']
        if metric['value'] is None and metric['reason']=='missing_or_incomplete_inputs':
            excluded=[r.get('_excluded_provenance',{}).get(f) for r in actual.values() for f in metric['input_fields'] if r.get('_excluded_provenance',{}).get(f)]
            if excluded:metric.update(reason='sec_input_excluded_by_quality_policy',excluded_input_facts=excluded)
        if name in {'gross_margin','operating_margin','net_margin','pe_annualized'} and latest.get('_source')=='sec' and latest.get('_sec_period_type')!='quarter':
            metric.update(value=None,reason='sec_period_not_a_single_quarter')
    return output
