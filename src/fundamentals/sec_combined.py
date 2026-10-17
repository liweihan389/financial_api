"""Source-aware querying. Explicit legacy generation/batch requests stay vendor-only."""
from collections import defaultdict
import datetime as dt
import json
import hashlib
from .service import FinancialService as VendorService, rows_from, json_safe, generation_of, limits, OPS
from .store import dictionaries
from .importer import METRIC_FIELDS
from .metrics import calculate, date, METRICS
from .currency import mixed_share_classes
from .sec_store import SecStore, iso
from .sec_mapping import rules_version, CUTOFF


def source_choice(data_source,generation=None,batch_id=None,sec_generation=None):
    source=data_source or ('compustat' if generation or batch_id else 'combined')
    if source not in {'combined','compustat','sec'}:raise ValueError('invalid_data_source')
    if source=='compustat' and sec_generation:raise ValueError('sec_generation_requires_sec_or_combined_source')
    if source=='combined' and (generation or batch_id) and not sec_generation:raise ValueError('combined_reproduction_requires_sec_generation_and_rule_version')
    return source


class FinancialService(VendorService):
    def __init__(self,root=None):
        super().__init__(root);self.sec=SecStore(self.store.root)
    def get_data_status(self,generation=None):
        return {**super().get_data_status(generation),'sec':self.sec.status(),'default_data_source':'combined'}
    def _pin(self,source,sec_generation=None,rule_version=None):
        if source=='compustat' or sec_generation=='none':return None
        manifest=self.sec.active()
        if sec_generation:
            with self.sec.connect(sec_generation) as c:manifest=json.loads(c.execute('SELECT payload FROM manifest').fetchone()[0])
        if rule_version and manifest and rule_version!=manifest['rule_version']:raise ValueError('sec_rule_version_mismatch')
        if manifest and not manifest['rule_version'].startswith('sec-standard-1-'):raise ValueError('sec_rules_unavailable_for_generation')
        return manifest
    def _supplements(self,keys,vendor,manifest,mode,as_of=None,share_policy='infer_compustat',respect_vendor_priority=True,identity_mapping=None):
        if not manifest:return []
        with self.sec.connect(manifest['sec_generation']) as c:
            cur=c.execute('SELECT * FROM observations WHERE gvkey IN ('+','.join('?' for _ in keys)+')',keys)
            cols=[d[0] for d in cur.description];observations=[dict(zip(cols,r)) for r in cur.fetchall()]
        contexts={}
        for row in vendor:contexts[row['gvkey']]=row
        candidates=defaultdict(list);excluded=defaultdict(dict)
        for o in observations:
            if identity_mapping is not None and identity_mapping.get(o['cik'])!=o['gvkey']:continue
            e=json.loads(o['evidence']);o['evidence']=e
            if mode=='historical_approx' and as_of and o['available']>str(as_of):continue
            if mode=='historical_approx' and o['quality']!='trusted':
                excluded[(o['gvkey'],o['period'])][o['field']]=dict(source='sec',quality=o['quality'],accession=o['accession'],available=o['available'],evidence=e,excluded_reason='inferred_input_excluded_in_historical_mode')
                continue
            if share_policy=='verified_only' and any('ads_equivalent' in s for s in e['reasons']):
                excluded[(o['gvkey'],o['period'])][o['field']]=dict(source='sec',quality=o['quality'],accession=o['accession'],evidence=e,excluded_reason='unverified_sec_ads_scale_excluded')
                continue
            context=contexts.get(o['gvkey'],{})
            currency=o['currency'] or context.get('curcdq')
            candidates[(o['gvkey'],o['period'],currency,e['fyearq'],e['fqtr'],e['fyr'],o['field'])].append(o)
        periods={}
        for key,choices in candidates.items():
            g,end,currency,year,q,fyr,field=key
            choices.sort(key=lambda o:(o['evidence']['formal'],o['available'],o['evidence'].get('accepted') or '',o['accession'],-o['evidence']['tag_priority']),reverse=True)
            o=choices[0];e=o['evidence']
            rank=(e['formal'],o['available'],e.get('accepted') or '',o['accession'],e['tag_priority'])
            same=[x for x in choices if (x['evidence']['formal'],x['available'],x['evidence'].get('accepted') or '',x['accession'],x['evidence']['tag_priority'])==rank]
            if len({x['value'] for x in same})>1:continue
            pk=key[:-1]
            if pk not in periods:
                ctx=contexts.get(g,{})
                periods[pk]={f:None for f in METRIC_FIELDS}
                periods[pk].update({f:ctx.get(f) for f in ['gvkey','conm','tic','exchg','fic','loc','priusa','prican','costat']})
                periods[pk].update(gvkey=g,datadate=iso(end),apdedateq=iso(end),curcdq=currency,fyearq=year,fqtr=q,fyr=fyr,_source='sec',_batch_id=manifest['sec_generation'],_source_row=0,_row_hash='sec:'+':'.join(map(str,pk)),_conflict=1,_period_conflict=1,_field_provenance={},_available_date=o['available'],_release_source=e['release_source'],_sec_period_type='quarter' if any(x['evidence']['period_type']=='quarter' for x in choices) else e['period_type'],_sec_known_share_conflict=mixed_share_classes(ctx),_sec_share_context='adr' if ctx.get('adrrq') is not None or ctx.get('priusa')=='90' else 'ordinary')
            row=periods[pk];row[field]=o['value'];row['_field_provenance'][field]=dict(source='sec',quality=o['quality'],accession=o['accession'],available=o['available'],evidence=e)
            if e.get('sec_share_scale_verified'):
                row['_sec_share_scale_verified']=True;row['_sec_adjustment_factor']=e['sec_adjustment_factor']
                row['_sec_share_scale_evidence']=e.get('share_scale_evidence')
            row['_available_date']=max(row['_available_date'],o['available'])
            if e['period_type']=='quarter':row['_sec_period_type']='quarter'
        output=[]
        conflicts=defaultdict(int)
        for key in periods:conflicts[key[:2]]+=1
        for r in periods.values():
            r['_period_conflict']=conflicts[(r['gvkey'],str(r['datadate']))]
            r['_excluded_provenance']=excluded[(r['gvkey'],str(r['datadate']))]
            # Whole vendor period wins, including NULL fields and conflicts.
            covered=any(v['gvkey']==r['gvkey'] and (date(v['datadate'])==r['datadate'] or (v.get('fyearq')==r['fyearq'] and v.get('fqtr')==r['fqtr'] and v.get('fyr')==r['fyr'] and abs((date(v.get('apdedateq') or v['datadate'])-r['datadate']).days)<=7)) for v in vendor)
            if covered and respect_vendor_priority:continue
            output.append(r)
        return output
    def merged_inputs(self,con,keys,batch_id=None,end=None,source='combined',manifest=None,mode='research',as_of=None,share_policy='infer_compustat',respect_vendor_priority=True):
        vendor=self._inputs(con,keys,batch_id,end)
        name=generation_of(con);self.sec.identity_audit(name)
        identity_mapping=self.sec._identity_cache[name][0]
        supplement=self._supplements(keys,vendor,manifest,mode,as_of,share_policy,respect_vendor_priority=source=='combined',identity_mapping=identity_mapping) if source!='compustat' else []
        if end:supplement=[r for r in supplement if r['datadate']<=date(end)]
        # SEC-only can use supplier prices/validation anchors, never supplier earnings.
        if source=='sec':
            anchors=[]
            for row in vendor:
                anchor=dict(row)
                for field in METRIC_FIELDS:
                    if field not in {'gvkey','datadate','prccq','ajexq','adrrq','conm','tic','exchg','fic','loc','curcdq','priusa','prican','costat'}:anchor[field]=None
                anchor['_price_only']=True;anchors.append(anchor)
            vendor=anchors
        return sorted(vendor+supplement,key=lambda r:(r['gvkey'],date(r['datadate'])))
    @staticmethod
    def _versions(result,source,manifest):
        result.update(data_source=source,sec_generation=manifest['sec_generation'] if manifest else None,rule_version=manifest['rule_version'] if manifest else None)
        manifest_tuple={'compustat_generation':result.get('database_generation'),'sec_generation':result['sec_generation'],'rule_version':result['rule_version']}
        result['release_manifest']={**manifest_tuple,'release_id':hashlib.sha256(json.dumps(manifest_tuple,sort_keys=True).encode()).hexdigest()[:24]}
        return result
    def get_financials(self,company,fields=None,start=None,end=None,mode='research',as_of=None,batch_id=None,limit=100,offset=0,quarterly_lag=90,annual_lag=120,generation=None,data_source=None,sec_generation=None,rule_version=None,**unused):
        source=source_choice(data_source,generation,batch_id,sec_generation);manifest=self._pin(source,sec_generation,rule_version);limits(limit,offset)
        if source=='combined' and (generation or batch_id) and not rule_version:raise ValueError('combined_reproduction_requires_rule_version')
        args=dict(company=company,fields=fields,start=start,end=end,mode=mode,as_of=as_of,batch_id=batch_id,quarterly_lag=quarterly_lag,annual_lag=annual_lag,generation=generation)
        if source=='compustat' or not manifest:
            result=super().get_financials(**args,limit=limit,offset=offset)
            if source=='sec':result['rows']=[]
            return self._versions(result,source,manifest)
        cutoff=date(as_of or end or dt.date.today())
        with self.store.connect(generation) as con:
            g=self._resolve(con,company);name=generation_of(con)
            result=super().get_financials(**{**args,'generation':name},limit=1000,offset=0)
            if source=='combined':
                page=result['rows']
                while len(page)==1000:
                    page=super().get_financials(**{**args,'generation':name},limit=1000,offset=len(result['rows']))['rows']
                    result['rows']+=page
            vendor=self._inputs(con,[g],batch_id)
            self.sec.identity_audit(name);identity_mapping=self.sec._identity_cache[name][0]
            supplement=self._supplements([g],vendor,manifest,mode,cutoff,respect_vendor_priority=source=='combined',identity_mapping=identity_mapping)
        selected=result['fields'];rows=result['rows'] if source=='combined' else []
        for row in rows:row['data_source']='compustat'
        for r in supplement:
            if start and r['datadate']<date(start) or end and r['datadate']>date(end):continue
            if mode=='historical_approx' and r['datadate']>cutoff:continue
            rows.append({**{k:r.get(k) for k in selected},'datadate':r['datadate'],'financial_currency':r['curcdq'],'fiscal_year':r['fyearq'],'fiscal_quarter':r['fqtr'],'release_date':r['_available_date'],'release_date_source':r['_release_source'],'source_batch_id':manifest['sec_generation'],'data_source':'sec','field_provenance':{k:v for k,v in {**r.get('_excluded_provenance',{}),**r['_field_provenance']}.items() if k in selected},'unavailable_fields':{k:r.get('_excluded_provenance',{}).get(k,{}).get('excluded_reason','no_validated_sec_fact_for_field') for k in selected if r.get(k) is None},'ambiguous_period':r['_period_conflict']>1})
        rows.sort(key=lambda r:str(r.get('datadate','')));result['total']=len(rows);result['rows']=rows[offset:offset+limit]
        return json_safe(self._versions(result,source,manifest))
    def get_metrics(self,company,as_of=None,start=None,end=None,metrics=None,mode='historical_approx',eps_basis='diluted',include_extraordinary=True,batch_id=None,quarterly_lag=90,annual_lag=120,max_price_age=120,limit=100,offset=0,generation=None,currency_policy='infer_curcdq',share_policy='infer_compustat',data_source=None,sec_generation=None,rule_version=None):
        source=source_choice(data_source,generation,batch_id,sec_generation);manifest=self._pin(source,sec_generation,rule_version);limits(limit,offset)
        if source=='combined' and (generation or batch_id) and not rule_version:raise ValueError('combined_reproduction_requires_rule_version')
        if not manifest and source!='sec':
            return super().get_metrics(company,as_of,start,end,metrics,mode,eps_basis,include_extraordinary,batch_id,quarterly_lag,annual_lag,max_price_age,limit,offset,generation,currency_policy,share_policy)
        selected=metrics or sorted(METRICS)
        if any(m not in METRICS for m in selected):raise ValueError('unknown_metric')
        with self.store.connect(generation) as con:
            g=self._resolve(con,company);name=generation_of(con)
            base=self.merged_inputs(con,[g],batch_id,end or as_of,source,manifest,'research',share_policy=share_policy)
            mappings=rows_from(con.execute('SELECT * FROM security_mappings WHERE gvkey=?',[g]))
            periods=[r for r in base if not r.get('_price_only')]
            points=[date(as_of)] if as_of else sorted({r['datadate'] for r in periods if (not start or date(r['datadate'])>=date(start)) and (not end or date(r['datadate'])<=date(end))}) if start or end else [max(date(r['datadate']) for r in periods)] if periods else []
            output=[]
            for point in points[offset:offset+limit]:
                inputs=self.merged_inputs(con,[g],batch_id,point,source,manifest,mode,point,share_policy)
                result=calculate(inputs,point,mode,eps_basis,include_extraordinary,quarterly_lag,annual_lag,max_price_age,mappings,currency_policy,share_policy)
                result['metrics']={k:result['metrics'][k] for k in selected};output.append(result)
            return json_safe(self._versions(dict(database_generation=name,gvkey=g,selected_batch_id=batch_id,currency_policy=currency_policy,share_policy=share_policy,total_dates=len(points),date_default='latest_company_period_not_today',results=output),source,manifest))
    def screen_companies(self,as_of,filters=None,market='US',mode='historical_approx',eps_basis='diluted',include_extraordinary=True,batch_id=None,quarterly_lag=90,annual_lag=120,max_price_age=120,limit=100,offset=0,generation=None,currency_policy='infer_curcdq',share_policy='infer_compustat',data_source=None,sec_generation=None,rule_version=None):
        source=source_choice(data_source,generation,batch_id,sec_generation);manifest=self._pin(source,sec_generation,rule_version)
        if source=='combined' and (generation or batch_id) and not rule_version:raise ValueError('combined_reproduction_requires_rule_version')
        args=dict(as_of=as_of,filters=filters,market=market,mode=mode,eps_basis=eps_basis,include_extraordinary=include_extraordinary,batch_id=batch_id,quarterly_lag=quarterly_lag,annual_lag=annual_lag,max_price_age=max_price_age,limit=limit,offset=offset,generation=generation,currency_policy=currency_policy,share_policy=share_policy)
        if source=='compustat' or not manifest:
            result=super().screen_companies(**args)
            if source=='sec':result.update(rows=[],total=0,eligible_before_filters=0)
            return self._versions(result,source,manifest)
        limits(limit,offset);point=date(as_of)
        if mode not in {'research','historical_approx'}:raise ValueError('strict_point_in_time_not_supported')
        if market not in {'US','CA','ALL'}:raise ValueError('invalid_market')
        filters=filters or []
        if len(filters)>20 or any(set(f)!={'metric','op','value'} or f['metric'] not in METRICS or f['op'] not in OPS or not isinstance(f['value'],(int,float)) for f in filters):raise ValueError('invalid_filter')
        with self.store.connect(generation) as con:
            name=generation_of(con);cachekey=('sec',name,manifest['sec_generation'],manifest['rule_version'],str(point),market,mode,eps_basis,include_extraordinary,batch_id,quarterly_lag,annual_lag,max_price_age,currency_policy,share_policy,source)
            with self._cache_lock:cached=self._screens.get(cachekey)
            if cached is None:
                allowed={'US':{11,12,14},'CA':{19},'ALL':None}[market]
                companies=con.execute('SELECT gvkey,exchg FROM metric_inputs QUALIFY row_number() OVER(PARTITION BY gvkey ORDER BY datadate DESC,_source_row)=1').fetchall()
                keys=[g for g,e in companies if allowed is None or e in allowed];found=[];excluded=defaultdict(int);mapping=defaultdict(list)
                for row in rows_from(con.execute('SELECT * FROM security_mappings')):mapping[row['gvkey']].append(row)
                for pos in range(0,len(keys),200):
                    subset=keys[pos:pos+200];groups=defaultdict(list)
                    for row in self.merged_inputs(con,subset,batch_id,point,source,manifest,mode,point,share_policy):groups[row['gvkey']].append(row)
                    for g in subset:
                        r=calculate(groups[g],point,mode,eps_basis,include_extraordinary,quarterly_lag,annual_lag,max_price_age,mapping[g],currency_policy,share_policy)
                        if r['financial_period'] is None:excluded['no_visible_financials']+=1;continue
                        flat={k:v for k,v in r.items() if k!='metrics'};flat.update({k:v['value'] for k,v in r['metrics'].items()});flat['unavailable_metrics']={k:v['reason'] for k,v in r['metrics'].items() if v['value'] is None};found.append(flat)
                cached=(found,dict(excluded),len(keys))
                with self._cache_lock:
                    self._screens[cachekey]=cached
                    while len(self._screens)>2:self._screens.popitem(last=False)
            candidates,excluded,total=cached
            found=[r for r in candidates if all(r[f['metric']] is not None and OPS[f['op']](r[f['metric']],f['value']) for f in filters)]
            return json_safe(self._versions(dict(database_generation=name,as_of=str(point),market=market,mode=mode,currency_policy=currency_policy,share_policy=share_policy,universe_count=total,eligible_before_filters=len(candidates),excluded_counts=excluded,total=len(found),rows=found[offset:offset+limit],warnings=['current_exchange_metadata','financial_only_filters_do_not_require_fresh_price']),source,manifest))
    def export_dataset(self,as_of,format='csv',max_rows=10000,**kwargs):
        # Pin both immutable layers before the base paginated exporter opens requests.
        source=source_choice(kwargs.get('data_source'),kwargs.get('generation'),kwargs.get('batch_id'),kwargs.get('sec_generation'))
        manifest=self._pin(source,kwargs.get('sec_generation'),kwargs.get('rule_version'))
        kwargs['data_source']=source
        kwargs.update(sec_generation=manifest['sec_generation'] if manifest else 'none',rule_version=manifest['rule_version'] if manifest else rules_version())
        if source=='compustat':kwargs.pop('sec_generation',None)
        return super().export_dataset(as_of,format,max_rows,**kwargs)
