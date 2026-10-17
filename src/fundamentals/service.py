from collections import defaultdict, OrderedDict
import csv
import datetime as dt
import io
import math
import json
import operator
from pathlib import Path
import threading

import duckdb

from .importer import METRIC_FIELDS
from .currency import CURRENCY_POLICIES, SHARE_POLICIES, evidence_status
from .metrics import METRICS, calculate, date
from .store import Store, dictionaries, ident, revision_sql, typed_projection, validate_batch

OPS={"eq":operator.eq,"ne":operator.ne,"gt":operator.gt,"gte":operator.ge,"lt":operator.lt,"lte":operator.le}

def rows_from(cursor):
    columns=[c[0] for c in cursor.description]
    return [dict(zip(columns,r)) for r in cursor.fetchall()]

def json_safe(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,(dt.datetime,dt.date)):
        return value.isoformat()
    if isinstance(value,dict):
        return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [json_safe(v) for v in value]
    return value

def generation_of(connection):
    return Path(connection.execute("PRAGMA database_list").fetchone()[2]).name

def limits(limit,offset):
    if not 1<=limit<=1000 or offset<0:
        raise ValueError("limit_must_be_1_to_1000_and_offset_nonnegative")

class FinancialService:
    def __init__(self,root=None):
        self.store=Store(root)
        self._screens=OrderedDict()
        self._cache_lock=threading.Lock()

    def get_data_status(self,generation=None):
        if generation is None and not self.store.is_initialized():
            return {"status":"not_initialized","current_rows":0,"database_generation":None,"imports":[],"metric_coverage":None,"verified_security_mappings":0,"strict_point_in_time_supported":False,"default_currency_policy":"infer_curcdq","default_share_policy":"infer_compustat","next_step":"upload_compustat_csv_or_zip_in_management_page",**evidence_status()}
        with self.store.connect(generation) as con:
            imports=rows_from(con.execute("SELECT batch_id,sequence,source_name,imported_at,report FROM imports ORDER BY sequence"))
            for row in imports:
                row["report"]=json.loads(row["report"])
                row["report"].pop("nonnull_counts",None)
            name=generation_of(con)
            coverage_path=self.store.root/f"coverage-{name}.json"
            policy=evidence_status()
            coverage=json.loads(coverage_path.read_text()) if coverage_path.exists() else None
            if coverage and any(coverage.get(k)!=v for k,v in policy.items()):
                coverage=None
            return json_safe({"status":"ready","database_generation":name,"imports":imports,"metric_coverage":coverage,"verified_security_mappings":con.execute("SELECT count(*) FROM security_mappings").fetchone()[0],"strict_point_in_time_supported":False,"current_rows":con.execute("SELECT count(*) FROM metric_inputs").fetchone()[0],"default_currency_policy":"infer_curcdq","default_share_policy":"infer_compustat",**policy})

    def get_field_dictionary(self,query=None,limit=100,offset=0,generation=None):
        limits(limit,offset)
        if generation is None and not self.store.is_initialized():
            template=json.loads(Path(__file__).with_name('compustat_schema.json').read_text())
            found=[d for d in template.values() if not query or query.lower() in (d['name']+' '+d['description']).lower()]
            return {'database_generation':None,'total':len(found),'fields':found[offset:offset+limit],'status':'schema_only_no_financial_data'}
        with self.store.connect(generation) as con:
            found=[d for d in dictionaries(con).values() if not query or query.lower() in (d["name"]+" "+d["description"]).lower()]
            return {"database_generation":generation_of(con),"total":len(found),"fields":found[offset:offset+limit]}

    def search_companies(self,query="",limit=100,offset=0,generation=None):
        limits(limit,offset)
        if generation is None and not self.store.is_initialized():
            return {'database_generation':None,'companies':[],'status':'not_initialized','next_step':'import_financial_data'}
        with self.store.connect(generation) as con:
            sql="""WITH companies AS (SELECT gvkey,conm,tic,costat,exchg,fic,curcdq,datadate,
                row_number() OVER(PARTITION BY gvkey ORDER BY datadate DESC,_source_row) AS rank
                FROM metric_inputs) SELECT * EXCLUDE(rank) FROM companies WHERE rank=1
                AND (contains(lower(conm),lower(?)) OR contains(lower(tic),lower(?)) OR contains(gvkey,?)) ORDER BY gvkey LIMIT ? OFFSET ?"""
            found=rows_from(con.execute(sql,[query,query,query,limit,offset]))
            return json_safe({"database_generation":generation_of(con),"companies":found,"metadata_basis":"current_company_metadata_not_historical_security_mapping"})

    def _resolve(self,con,company):
        exact=con.execute("SELECT DISTINCT gvkey FROM metric_inputs WHERE gvkey=?",[company]).fetchall()
        if exact:
            return exact[0][0]
        candidates=con.execute("SELECT DISTINCT gvkey,conm FROM metric_inputs WHERE upper(tic)=upper(?) ORDER BY gvkey",[company]).fetchall()
        if not candidates:
            raise ValueError("company_not_found")
        keys={r[0] for r in candidates}
        if len(keys)!=1:
            raise ValueError("ambiguous_company_code: "+json.dumps([{"gvkey":k,"company":n} for k,n in candidates],ensure_ascii=False))
        return candidates[0][0]

    def _inputs(self,con,gvkeys,batch_id=None,end=None,recent=False):
        validate_batch(con,batch_id)
        placeholders=",".join("?" for _ in gvkeys)
        args=list(gvkeys)
        if batch_id is None:
            sql=f"SELECT * FROM metric_inputs WHERE gvkey IN ({placeholders})"
        else:
            dictionary=dictionaries(con)
            revision,parameters=revision_sql(batch_id,gvkeys)
            fields=[f for f in METRIC_FIELDS if f in dictionary]
            projection=typed_projection(fields,dictionary)
            missing=[f"NULL AS {ident(f)}" for f in METRIC_FIELDS if f not in dictionary]
            if missing:
                projection+=", "+", ".join(missing)
            sql=f"""WITH current AS ({revision}), inputs AS (SELECT {projection},_batch_id,_source_row,_row_hash,_business_key,
                count(*) OVER(PARTITION BY _business_key) AS _conflict,
                count(*) OVER(PARTITION BY gvkey,datadate) AS _period_conflict FROM current WHERE _valid_key)
                SELECT * FROM inputs WHERE gvkey IN ({placeholders})"""
            args=parameters+args
        if end:
            sql+=" AND datadate<=?"
            args.append(date(end))
        if recent:
            sql+=" QUALIFY row_number() OVER(PARTITION BY gvkey ORDER BY datadate DESC,_source_row)<=16"
        sql+=" ORDER BY gvkey,datadate,_source_row"
        return rows_from(con.execute(sql,args))

    def get_financials(self,company,fields=None,start=None,end=None,mode="research",as_of=None,batch_id=None,limit=100,offset=0,quarterly_lag=90,annual_lag=120,generation=None):
        limits(limit,offset)
        if mode not in {"research","historical_approx"}:
            raise ValueError("strict_point_in_time_not_supported")
        with self.store.connect(generation) as con:
            validate_batch(con,batch_id)
            gvkey=self._resolve(con,company)
            dictionary=dictionaries(con)
            selected=fields or [f for f in METRIC_FIELDS if f in dictionary]
            if not selected or any(f not in dictionary for f in selected) or len(selected)>679:
                raise ValueError("unknown_or_invalid_field_selection")
            revision,args=revision_sql(batch_id,[gvkey])
            # Scope the key before conversion. The latest-version ranking still
            # covers all variants of this company's business keys.
            conditions=["gvkey=?","_valid_key"]
            args.append(gvkey)
            if start:
                conditions.append("try_cast(datadate AS DATE)>=?")
                args.append(date(start))
            if end:
                conditions.append("try_cast(datadate AS DATE)<=?")
                args.append(date(end))
            cutoff=date(as_of or end or dt.date.today())
            if mode=="historical_approx":
                conditions.append("try_cast(datadate AS DATE)<=?")
                args.append(cutoff)
                conditions.append("coalesce(greatest(try_cast(rdq AS DATE),try_cast(fdateq AS DATE)),coalesce(try_cast(apdedateq AS DATE),try_cast(datadate AS DATE)) + CASE WHEN try_cast(fqtr AS DOUBLE)=4 THEN ? ELSE ? END::INTEGER) <= ?")
                args.extend([annual_lag,quarterly_lag,cutoff])
            projection=typed_projection(selected,dictionary)
            release_sql="coalesce(greatest(try_cast(rdq AS DATE),try_cast(fdateq AS DATE)),coalesce(try_cast(apdedateq AS DATE),try_cast(datadate AS DATE)) + CASE WHEN try_cast(fqtr AS DOUBLE)=4 THEN "+str(int(annual_lag))+" ELSE "+str(int(quarterly_lag))+" END::INTEGER)"
            release_source_sql="CASE WHEN try_cast(rdq AS DATE) IS NOT NULL OR try_cast(fdateq AS DATE) IS NOT NULL THEN CASE WHEN try_cast(fdateq AS DATE) IS NOT NULL AND (try_cast(rdq AS DATE) IS NULL OR try_cast(fdateq AS DATE)>=try_cast(rdq AS DATE)) THEN 'fdateq' ELSE 'rdq' END ELSE CASE WHEN try_cast(fqtr AS DOUBLE)=4 THEN 'estimated_"+str(int(annual_lag))+"_days' ELSE 'estimated_"+str(int(quarterly_lag))+"_days' END END"
            sql=f"WITH current AS ({revision}) SELECT {projection},curcdq AS financial_currency,try_cast(fyearq AS DOUBLE) AS fiscal_year,try_cast(fqtr AS DOUBLE) AS fiscal_quarter,{release_sql} AS release_date,{release_source_sql} AS release_date_source, count(*) OVER(PARTITION BY gvkey,try_cast(datadate AS DATE))>1 AS ambiguous_period, _batch_id AS source_batch_id,_source_row AS source_row,_row_hash AS source_row_hash FROM current WHERE {' AND '.join(conditions)} ORDER BY datadate,_source_row LIMIT ? OFFSET ?"
            found=rows_from(con.execute(sql,args+[limit,offset]))
            return json_safe({"database_generation":generation_of(con),"gvkey":gvkey,"mode":mode,"as_of":str(cutoff) if mode=="historical_approx" else None,"batch_id":batch_id,"fields":selected,"rows":found,"warnings":["revised_snapshot_not_point_in_time"]})

    def get_metrics(self,company,as_of=None,start=None,end=None,metrics=None,mode="historical_approx",eps_basis="diluted",include_extraordinary=True,batch_id=None,quarterly_lag=90,annual_lag=120,max_price_age=120,limit=100,offset=0,generation=None,currency_policy="infer_curcdq",share_policy="infer_compustat"):
        limits(limit,offset)
        if currency_policy not in CURRENCY_POLICIES:
            raise ValueError("invalid_currency_policy")
        if share_policy not in SHARE_POLICIES:
            raise ValueError("invalid_share_policy")
        selected=metrics or sorted(METRICS)
        if any(m not in METRICS for m in selected):
            raise ValueError("unknown_metric")
        with self.store.connect(generation) as con:
            gvkey=self._resolve(con,company)
            inputs=self._inputs(con,[gvkey],batch_id,end or as_of)
            mappings=rows_from(con.execute("SELECT * FROM security_mappings WHERE gvkey=?",[gvkey]))
            if as_of:
                dates=[date(as_of)]
            elif start or end:
                dates=sorted({date(r["datadate"]) for r in inputs if r.get("prccq") is not None and (not start or date(r["datadate"])>=date(start)) and (not end or date(r["datadate"])<=date(end))})
            else:
                dates=[max(date(r["datadate"]) for r in inputs)] if inputs else []
            output=[]
            for point in dates[offset:offset+limit]:
                result=calculate(inputs,point,mode,eps_basis,include_extraordinary,quarterly_lag,annual_lag,max_price_age,mappings,currency_policy,share_policy)
                result["metrics"]={m:result["metrics"][m] for m in selected}
                output.append(result)
            return json_safe({"database_generation":generation_of(con),"gvkey":gvkey,"selected_batch_id":batch_id,"total_dates":len(dates),"date_default":"latest_company_period_not_today","currency_policy":currency_policy,"share_policy":share_policy,**evidence_status(),"results":output})

    def screen_companies(self,as_of,filters=None,market="US",mode="historical_approx",eps_basis="diluted",include_extraordinary=True,batch_id=None,quarterly_lag=90,annual_lag=120,max_price_age=120,limit=100,offset=0,generation=None,currency_policy="infer_curcdq",share_policy="infer_compustat"):
        limits(limit,offset)
        if currency_policy not in CURRENCY_POLICIES:
            raise ValueError("invalid_currency_policy")
        if share_policy not in SHARE_POLICIES:
            raise ValueError("invalid_share_policy")
        date(as_of)
        if market not in {"US","CA","ALL"} or mode not in {"research","historical_approx"}:
            raise ValueError("strict_point_in_time_not_supported" if mode=="strict" else "invalid_market_or_time_mode")
        filters=filters or []
        if len(filters)>20:
            raise ValueError("too_many_filters")
        for f in filters:
            if set(f)!={"metric","op","value"} or f["metric"] not in METRICS or f["op"] not in OPS or not isinstance(f["value"],(int,float)):
                raise ValueError("invalid_filter: use metric/op/value with an allowed numeric metric")
        with self.store.connect(generation) as con:
            validate_batch(con,batch_id)
            name=generation_of(con)
            cachekey=(name,str(as_of),market,mode,eps_basis,include_extraordinary,batch_id,quarterly_lag,annual_lag,max_price_age,currency_policy,share_policy)
            with self._cache_lock:
                cached=self._screens.get(cachekey)
            if cached is None:
                companies=con.execute("""SELECT gvkey,exchg FROM metric_inputs
                    QUALIFY row_number() OVER(PARTITION BY gvkey ORDER BY datadate DESC,_source_row)=1 ORDER BY gvkey""").fetchall()
                allowed={"US":{11,12,14},"CA":{19},"ALL":None}[market]
                keys=[g for g,exchange in companies if allowed is None or (exchange is not None and float(exchange) in allowed)]
                mapping_index=defaultdict(list)
                for m in rows_from(con.execute("SELECT * FROM security_mappings")):
                    mapping_index[m["gvkey"]].append(m)
                cached=[]
                exclusion_counts=defaultdict(int)
                for pos in range(0,len(keys),200):
                    grouped=defaultdict(list)
                    # Keep all earlier periods: delayed/revised release dates
                    # can make more than 16 recent rows unavailable, and the
                    # last complete fiscal year may precede that window.
                    for row in self._inputs(con,keys[pos:pos+200],batch_id,as_of,recent=False):
                        grouped[row["gvkey"]].append(row)
                    for gvkey in keys[pos:pos+200]:
                        rows=grouped.get(gvkey,[])
                        if not rows:
                            exclusion_counts["no_period_before_as_of"]+=1
                            continue
                        result=calculate(rows,as_of,mode,eps_basis,include_extraordinary,quarterly_lag,annual_lag,max_price_age,mapping_index[gvkey],currency_policy,share_policy)
                        if result["financial_period"] is None or result["price_date"] is None or result["price_age_days"]>max_price_age:
                            exclusion_counts["no_visible_financials_or_fresh_price"]+=1
                            continue
                        flat={k:result[k] for k in ["gvkey","ticker","company","as_of","mode","financial_period","financial_currency","release_date","release_date_source","price_date","price","price_age_days","batch_id","valuation_status","warnings"]}
                        flat.update({k:result[k] for k in ["currency_policy","price_currency","price_currency_source","price_currency_verified","price_currency_evidence","price_market","price_trading_date","share_basis_status","share_policy","share_basis_verified","adr_ratio","adr_ratio_applied","share_basis_evidence"]})
                        flat.update({k:v["value"] for k,v in result["metrics"].items()})
                        flat["unavailable_metrics"]={k:v["reason"] for k,v in result["metrics"].items() if v["value"] is None}
                        cached.append(flat)
                cached=(cached,dict(exclusion_counts),len(keys))
                with self._cache_lock:
                    self._screens[cachekey]=cached
                    while len(self._screens)>2:
                        self._screens.popitem(last=False)
            candidates,exclusions,universe_count=cached
            found=[r for r in candidates if all(r[f["metric"]] is not None and OPS[f["op"]](r[f["metric"]],f["value"]) for f in filters)]
            return json_safe({"database_generation":name,"as_of":str(as_of),"market":market,"mode":mode,"currency_policy":currency_policy,"share_policy":share_policy,**evidence_status(),"universe_count":universe_count,"eligible_before_filters":len(candidates),"excluded_counts":exclusions,"total":len(found),"rows":found[offset:offset+limit],"warnings":["revised_snapshot_not_point_in_time","market_classification_uses_current_exchange_metadata","inactive_companies_not_excluded_by_current_status","currency_verification_does_not_certify_share_basis"]})

    def export_dataset(self,as_of,format="csv",max_rows=10000,**kwargs):
        if format not in {"csv","parquet"} or not 1<=max_rows<=100000:
            raise ValueError("invalid_export_format_or_row_limit")
        kwargs.pop("limit",None)
        kwargs.pop("offset",None)
        first=self.screen_companies(as_of,limit=min(max_rows,1000),offset=0,**kwargs)
        pinned=first["database_generation"]
        kwargs["generation"]=pinned
        rows=first["rows"]
        for offset in range(1000,min(first["total"],max_rows),1000):
            rows+=self.screen_companies(as_of,limit=min(1000,max_rows-offset),offset=offset,**kwargs)["rows"]
        metadata={k:v for k,v in first.items() if k!="rows"}
        metadata.update({"exported_rows":len(rows),"truncated":first["total"]>max_rows})
        columns=["gvkey","ticker","company","as_of","mode","financial_period","financial_currency","release_date","release_date_source","price_date","price","price_age_days","batch_id","valuation_status","warnings","unavailable_metrics","currency_policy","price_currency","price_currency_source","price_currency_verified","price_currency_evidence","price_market","price_trading_date","share_basis_status","share_policy","share_basis_verified","adr_ratio","adr_ratio_applied","share_basis_evidence"]+sorted(METRICS)
        columns+=sorted(set().union(*(r.keys() for r in rows))-set(columns)) if rows else []
        flattened=[{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows]
        if format=="csv":
            output=io.StringIO()
            writer=csv.DictWriter(output,fieldnames=columns)
            writer.writeheader()
            writer.writerows(flattened)
            return output.getvalue().encode("utf-8-sig"),metadata
        # DuckDB writes Parquet without a pandas/pyarrow dependency.
        import tempfile
        with tempfile.TemporaryDirectory() as working,duckdb.connect() as con:
            path=Path(working)/"export.parquet"
            con.execute("CREATE TABLE exported("+",".join(f'{ident(c)} '+("DOUBLE" if c in METRICS or c in {"price","price_age_days","adr_ratio"} else "VARCHAR") for c in columns)+")")
            if flattened:
                con.executemany("INSERT INTO exported VALUES ("+",".join("?" for _ in columns)+")",[[r.get(c) for c in columns] for r in flattened])
            con.execute("COPY exported TO ? (FORMAT PARQUET)",[str(path)])
            return path.read_bytes(),metadata
