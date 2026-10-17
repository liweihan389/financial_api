import bisect
from contextlib import contextmanager
import threading
import datetime as dt
import json
import math
from pathlib import Path
import uuid
import duckdb
from .market_store import MarketStore,POLICY
from .metrics import calculate,availability,date,number,METRICS
from .service import rows_from,json_safe,limits,OPS,generation_of
from .sec_combined import FinancialService,source_choice
from .technical import indicators,return_statistics
from .price_adjustment import adjusted_ohlc,resolve_basis,POLICY as ADJUSTMENT_POLICY
from .store import literal

class MarketService:
    def __init__(self,root=None):
        self.store=MarketStore(root);self.financial=FinancialService(root)
        self._company_indexes={};self._index_lock=threading.Lock()
    @contextmanager
    def _connections(self,generation,financial_generation,context=None):
        if context is not None:
            yield context['market_connection'],context['manifest'],context['financial_connection']
        else:
            with self.store.connect(generation) as (c,m),self.financial.store.connect(financial_generation) as f:yield c,m,f
    def _company_index(self,f):
        name=generation_of(f)
        with self._index_lock:
            if name not in self._company_indexes:
                index={}
                for gvkey,cusip in f.execute('SELECT DISTINCT gvkey,cusip FROM financial_current WHERE cusip IS NOT NULL').fetchall():index.setdefault(cusip[:8],[]).append({'gvkey':gvkey,'cusip':cusip})
                self._company_indexes[name]=index
                while len(self._company_indexes)>2:self._company_indexes.pop(next(iter(self._company_indexes)))
            return self._company_indexes[name]
    def status(self,generation=None):
        m=self.store.manifest(generation)
        return {'status':'ready' if m else 'not_initialized','market_generation':m['generation'] if m else None,'policy_version':POLICY,'scope':m['scope'] if m else None,'summary':m.get('summary') if m else None,'imports':[{k:v for k,v in b.items() if k not in {'files','fields'}} for b in m['batches']] if m else [],'measurements':m.get('import_measurements') if m else None,'manual_link_count':m.get('manual_link_count',0) if m else 0,'adjustment_defaults':{'general':'total_return','technical':'price_return','explicit_basis_overrides_purpose':True,'directions':['forward','backward'],'policy_version':ADJUSTMENT_POLICY},'warnings':['sample_data_not_full_market_history'] if m and m['scope']!='full' else []}
    def dictionary(self,query=None,limit=100,offset=0,generation=None):
        limits(limit,offset)
        from .market_store import NUMERIC_FIELDS
        m=self.store.manifest(generation)
        if not m:return {'market_generation':None,'fields':[],'total':0,'status':'market_not_initialized'}
        descriptions={'DlyPrc':'未复权日价；结合 DlyPrcFlg 区分成交价与报价均值','DlyClose':'交易日最后成交价格','DlyRet':'包含股息的供应商日总收益率','DlyRetx':'不含普通现金股息的日价格收益率','DlyFacPrc':'当日价格调整因子；不是累计复权因子','CUSIP':'该有效期间的证券 CUSIP，8 位','CUSIP9':'该有效期间的证券 CUSIP，9 位','HdrCUSIP':'当前头部 CUSIP；不代表全部历史','HdrCUSIP9':'当前头部 CUSIP，9 位','PERMNO':'永久证券标识；保留字符串','PERMCO':'发行人标识；不区分股类','DisOrigCurType':'分配原币种；不是报价币种','ShrOut':'供应商股份数量；单位尚待独立核验，不直接用于本版公司估值','DlyCap':'供应商证券级市值，千美元；不等同于多股类公司总市值'}
        fields=[]
        for name in m['batches'][-1]['fields']:
            description=descriptions.get(name,'CRSP CIZ 导出字段；定义参照随附字段清单与供应商指南')
            if query and query.lower() not in (name+' '+description).lower():continue
            fields.append({'name':name,'raw_storage_type':'VARCHAR','query_type':'DOUBLE' if name in NUMERIC_FIELDS else 'VARCHAR','description':description,'definition_source':'crsp2525(daily).pdf / CRSP CIZ guide','event_field':name.startswith('Dis')})
        return {'market_generation':m['generation'],'total':len(fields),'fields':fields[offset:offset+limit]}
    def _resolve(self,c,security):
        if c.execute('SELECT 1 FROM daily WHERE PERMNO=? LIMIT 1',[security]).fetchone():return security
        keys=c.execute('SELECT DISTINCT PERMNO FROM daily WHERE upper(Ticker)=upper(?) OR upper(TradingSymbol)=upper(?)',[security,security]).fetchall()
        if len(keys)!=1:raise ValueError('ambiguous_security_use_permno' if keys else 'security_not_found')
        return keys[0][0]
    def search(self,query='',as_of=None,limit=100,offset=0,generation=None):
        limits(limit,offset)
        with self.store.connect(generation) as (c,m):
            args=[];condition=''
            if as_of:condition='WHERE DlyCalDt<=?';args=[str(as_of)]
            sql=f'''WITH latest AS (SELECT * FROM daily {condition} QUALIFY row_number() OVER(PARTITION BY PERMNO ORDER BY DlyCalDt DESC)=1)
             SELECT PERMNO,PERMCO,Ticker,TradingSymbol,IssuerNm,ShareClass,CUSIP9,HdrCUSIP9,SecurityType,SecuritySubType,ShareType,PrimaryExch,TradingStatusFlg,DlyCalDt FROM latest
             WHERE contains(lower(coalesce(IssuerNm,'')),lower(?)) OR contains(lower(coalesce(Ticker,'')),lower(?)) OR contains(PERMNO,?) ORDER BY PERMNO LIMIT ? OFFSET ?'''
            rows=rows_from(c.execute(sql,args+[query,query,query,limit,offset]))
            return {'market_generation':m['generation'],'scope':m['scope'],'rows':rows}
    def prices(self,security,start=None,end=None,fields=None,limit=100,offset=0,generation=None,events=False):
        limits(limit,offset)
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        with self.store.connect(generation) as (c,m):
            permno=self._resolve(c,security)
            allowed=set(m['batches'][-1]['fields'])|{'_batch_id','_source_sha256','_source_member','_source_row','_quote_conflict','_invalid_numeric_fields'}
            fields=fields or (['PERMNO','DlyCalDt','DisSeqNbr','DisType','DisDivAmt','DisFacPr','DisFacShr'] if events else ['PERMNO','DlyCalDt','Ticker','TradingSymbol','CUSIP9','ShareClass','DlyPrc','DlyPrcFlg','DlyClose','DlyHigh','DlyLow','DlyOpen','DlyVol','DlyRet','DlyRetx','DlyFacPrc','TradingStatusFlg','_quote_conflict','_invalid_numeric_fields','_batch_id','_source_row'])
            if not events:allowed={f for f in allowed if not f.startswith('Dis')}
            if not fields or any(f not in allowed or (events and f=='_quote_conflict') for f in fields):raise ValueError('unknown_market_field')
            where=['PERMNO=?'];args=[permno]
            if start:where.append('DlyCalDt>=?');args.append(str(start))
            if end:where.append('DlyCalDt<=?');args.append(str(end))
            table='events' if events else 'daily'
            total=c.execute(f"SELECT count(*) FROM {table} WHERE "+' AND '.join(where),args).fetchone()[0]
            projection=','.join('"'+f+'"' for f in fields)
            rows=rows_from(c.execute(f'SELECT {projection} FROM {table} WHERE '+ ' AND '.join(where)+' ORDER BY DlyCalDt,_source_row LIMIT ? OFFSET ?',args+[limit,offset]))
            return {'market_generation':m['generation'],'scope':m['scope'],'permno':permno,'total':total,'rows':rows,'fields':fields}
    def _history(self,c,permno,end=None,start=None,warmup=600):
        args=[permno];sql='SELECT * FROM daily WHERE PERMNO=?'
        if end:sql+=' AND DlyCalDt<=?';args.append(str(end))
        if start:
            sql+=' AND (DlyCalDt>=? OR DlyCalDt IN (SELECT DlyCalDt FROM daily WHERE PERMNO=? AND DlyCalDt<? ORDER BY DlyCalDt DESC LIMIT ?))'
            args += [str(start),permno,str(start),warmup]
        sql+=' ORDER BY DlyCalDt LIMIT 100001'
        rows=rows_from(c.execute(sql,args))
        if len(rows)>100000:raise ValueError('security_history_limit_exceeded')
        return rows
    def adjusted_prices(self,security,start=None,end=None,limit=100,offset=0,generation=None,basis=None,adjustment='forward',purpose='general'):
        limits(limit,offset)
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        effective_basis=resolve_basis(basis,purpose)
        if adjustment not in {'forward','backward'}:raise ValueError('invalid_adjustment_direction')
        with self.store.connect(generation) as (c,m):
            permno=self._resolve(c,security);raw=self._history(c,permno,end,start,warmup=0)
            rows=adjusted_ohlc(raw,effective_basis,adjustment,purpose)
            expected_anchor=raw[-1 if adjustment=='forward' else 0]['DlyCalDt'] if raw else None
            warnings=[]
            if any(row['anchor_date']!=expected_anchor for row in rows):warnings.append('discontinuous_segments_have_separate_anchors')
            if m['scope']!='full':warnings.append('sample_data_not_full_market_history')
            return json_safe({'market_generation':m['generation'],'scope':m['scope'],'permno':permno,'basis':effective_basis,'basis_source':'explicit' if basis is not None else 'purpose_default','purpose':purpose,'adjustment':adjustment,'adjustment_policy_version':ADJUSTMENT_POLICY,'ordinary_cash_dividends_included':effective_basis=='total_return','source_return_field':'DlyRet' if effective_basis=='total_return' else 'DlyRetx','expected_anchor_date':expected_anchor,'definitions':{'forward':'last observed close is raw; A[t-1]=A[t]/(1+return[t])','backward':'first observed close is raw; A[t]=A[t-1]*(1+return[t])','ohlc':'raw OHLC times adjusted_price / DlyPrc; close uses DlyPrc','volume':'raw daily volume, unadjusted'},'total':len(rows),'rows':rows[offset:offset+limit],'warnings':warnings})
    def technical(self,security,start=None,end=None,limit=100,offset=0,generation=None,**parameters):
        limits(limit,offset)
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        with self.store.connect(generation) as (c,m):
            permno=self._resolve(c,security)
            rolling_periods=list(parameters.get('sma',[20,60,120,250]))+list(parameters.get('windows',[21,63,126,252]))+[parameters.get('bb_period',20)]
            smooth_periods=list(parameters.get('ema_periods',[12,26]))+list(parameters.get('macd',[12,26,9]))+[parameters.get('rsi_period',14),parameters.get('atr_period',14)]
            warmup=max(600,max(rolling_periods)+1,10*max(smooth_periods))
            raw=self._history(c,permno,end,start,warmup)
            result=indicators(raw,**parameters)
            selected=[r for r in result if not start or r['date']>=str(start)]
            return {'market_generation':m['generation'],'scope':m['scope'],'permno':permno,'basis':parameters.get('basis','price_return'),'parameters':parameters,'definitions':{'ema':'SMA seed; alpha=2/(n+1)','rsi_atr':'Wilder SMA seed, recursive alpha=1/n','bollinger_std':'population standard deviation','volatility':'sample standard deviation times sqrt(annual_days)','momentum':'adjusted close / n-days-earlier adjusted close - 1'},'total':len(selected),'rows':selected[offset:offset+limit]}
    def returns(self,security,start=None,end=None,generation=None,basis='total_return',annual_days=252):
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        with self.store.connect(generation) as (c,m):
            permno=self._resolve(c,security);rows=[r for r in self._history(c,permno,end) if not start or r['DlyCalDt']>=str(start)]
            return {'market_generation':m['generation'],'scope':m['scope'],'permno':permno,'start':rows[0]['DlyCalDt'] if rows else None,'end':rows[-1]['DlyCalDt'] if rows else None,**return_statistics(rows,basis,annual_days)}
    def daily_metrics(self,security,as_of,mode='historical_approx',generation=None,financial_generation=None,batch_id=None,eps_basis='diluted',include_extraordinary=True,quarterly_lag=90,annual_lag=120,max_price_age=5,currency_policy='infer_curcdq',share_policy='infer_compustat',data_source=None,sec_generation=None,rule_version=None,_context=None):
        if mode not in {'research','historical_approx'}:raise ValueError('strict_point_in_time_not_supported')
        source=source_choice(data_source,financial_generation,batch_id,sec_generation)
        sec_manifest=self.financial._pin(source,sec_generation,rule_version)
        if source=='combined' and (financial_generation or batch_id) and not rule_version:raise ValueError('combined_reproduction_requires_rule_version')
        with self._connections(generation,financial_generation,_context) as (c,m,f):
            permno=self._resolve(c,security);history=self._history(c,permno,as_of)
            if not history:raise ValueError('no_price_before_as_of')
            last=history[-1]
            valid=[r for r in history if number(r.get('DlyPrc')) is not None and number(r.get('DlyPrc'))>0 and r.get('DlyPrcFlg')=='TR' and not r['_quote_conflict']]
            price=valid[-1] if valid else last
            candidates=set()
            for r in history:
                for k in ('CUSIP','CUSIP9','HdrCUSIP','HdrCUSIP9'):
                    if r.get(k):candidates.add(r[k][:8])
            index=self._company_index(f)
            point_cusip=str(price.get('CUSIP') or price.get('CUSIP9') or '')[:8]
            exact_candidates=index.get(point_cusip,[])
            segment=[]
            for observation in reversed(history):
                if observation['DlyCalDt']>price['DlyCalDt']:continue
                if observation.get('PERMCO')!=price.get('PERMCO') or observation.get('ShareClass')!=price.get('ShareClass') or observation.get('ShareType')!=price.get('ShareType'):break
                segment.append(observation)
            candidates={str(r[k])[:8] for r in segment for k in ('CUSIP','CUSIP9','HdrCUSIP','HdrCUSIP9') if r.get(k)}
            matches=exact_candidates or list({r['gvkey']:r for key in candidates for r in index.get(key,[])}.values())
            from .market_links import matching_links
            manual=matching_links(self.store,m,permno,price['DlyCalDt'])
            if manual:matches=[{'gvkey':r['gvkey'],'cusip':'','evidence':r['evidence']} for r in manual]
            metadata={'market_generation':m['generation'],'database_generation':generation_of(f),'scope':m['scope'],'permno':permno,'price_date':price['DlyCalDt'],'price':number(price.get('DlyPrc')),'price_flag':price.get('DlyPrcFlg'),'price_currency':'USD','price_currency_source':'CRSP_US_stock_price_definition','price_currency_verified':False,'matching_candidates':matches,'data_source':source,'sec_generation':sec_manifest['sec_generation'] if sec_manifest else None,'rule_version':sec_manifest['rule_version'] if sec_manifest else None}
            issuer_only=False
            if not matches and point_cusip:
                matches=list({r['gvkey']:r for key,values in index.items() if key[:6]==point_cusip[:6] for r in values}.values());issuer_only=bool(matches)
                metadata['matching_candidates']=matches
            if len(matches)!=1 or issuer_only:
                return {**metadata,'matching_status':'issuer_candidate_only' if issuer_only else 'ambiguous' if matches else 'unmatched','metrics':{k:{'value':None,'reason':'issuer_prefix_not_security_mapping' if issuer_only else 'ambiguous_company_mapping' if matches else 'unmatched_company'} for k in sorted(METRICS)}}
            gvkey=matches[0]['gvkey'];inputs=self.financial.merged_inputs(f,[gvkey],batch_id,as_of,source,sec_manifest,mode,as_of,share_policy)
            calendar=_context['calendar'] if _context is not None else [r[0] for r in c.execute('SELECT DISTINCT DlyCalDt FROM daily ORDER BY DlyCalDt').fetchall()]
            def effective(row):
                released,src=availability(row,quarterly_lag,annual_lag);j=bisect.bisect_right(calendar,str(released))
                next_day=calendar[j] if j<len(calendar) else '9999-12-31'
                return next_day,released,src
            visible=[r for r in inputs if not r.get('_price_only') and (mode=='research' or effective(r)[0]<=str(as_of))]
            if not visible:return {**metadata,'gvkey':gvkey,'matching_status':'candidate','metrics':{k:{'value':None,'reason':'no_visible_financials'} for k in sorted(METRICS)}}
            latest=visible[-1]
            anchors=[r for r in inputs if r.get('prccq') is not None and r.get('_source')!='sec']
            validation=anchors[-1] if anchors else latest
            anchor=[r for r in history if r['DlyCalDt']<=str(validation['datadate'])]
            anchor=anchor[-1] if anchor else None
            source_price=number(validation.get('prccq'));observed=number(anchor.get('DlyPrc')) if anchor else None
            reason='unverified_daily_price_currency' if currency_policy=='verified_only' else None
            if latest.get('curcdq')!='USD':reason='price_and_financial_currency_mismatch'
            elif last.get('TradingStatusFlg')!='A' or last.get('DlyDelFlg')=='Y':reason='suspended_or_inactive_security'
            elif number(price.get('DlyPrc')) is None or number(price.get('DlyPrc'))<=0 or price.get('DlyPrcFlg')!='TR' or price.get('_quote_conflict') or price.get('DlyDelFlg')=='Y':reason='invalid_daily_trade_price'
            elif (date(as_of)-date(price['DlyCalDt'])).days>max_price_age:reason='stale_price'
            elif anchor is None or (date(validation['datadate'])-date(anchor['DlyCalDt'])).days>5:reason='missing_quarter_price_anchor'
            elif not manual and anchor.get('PERMCO')!=price.get('PERMCO'):reason='quarter_anchor_issuer_identity_changed'
            elif not source_price or observed is None or abs(observed/source_price-1)>0.005:reason='financial_price_share_basis_mismatch'
            elif price.get('ShareType')=='AD' and (number(validation.get('adrrq')) is None or number(validation.get('adrrq'))<=0):reason='missing_or_invalid_adr_ratio'
            elif price.get('ShareType')=='NS' and number(validation.get('adrrq')) not in {None,1}:reason='financial_price_share_type_mismatch'
            elif price.get('SecurityType')!='EQTY' or price.get('SecuritySubType')!='COM' or price.get('ShareType') not in {'NS','AD'} or price.get('ConditionalType')!='RW':reason='non_common_equity_valuation'
            elif not (str(price.get('SecInfoStartDt') or '')<=price['DlyCalDt']<=str(price.get('SecInfoEndDt') or '')):reason='invalid_security_history_interval'
            split=1.0
            for row in history:
                if str(latest['datadate'])<row['DlyCalDt']<=price['DlyCalDt']:
                    fac=number(row.get('DlyFacPrc'))
                    if fac is not None and fac!=1:
                        event_types={r[0] for r in c.execute('SELECT DisType FROM events WHERE PERMNO=? AND DlyCalDt=?',[permno,row['DlyCalDt']]).fetchall()}
                        if fac<=0 or not event_types or not event_types.issubset({'FRS','SD'}):reason=reason or 'unverified_non_split_share_adjustment'
                        else:split*=fac
            transformed=[]
            for row in inputs:
                effective_day,original,source=effective(row)
                transformed.append({**row,'rdq':date(effective_day),'fdateq':None,**({'_available_date':effective_day} if row.get('_source')=='sec' else {}),'_original_release':str(original),'_release_origin':source})
            external={**validation,'datadate':date(price['DlyCalDt']),'prccq':number(price.get('DlyPrc')),'ajexq':number(validation.get('ajexq'))/split if number(validation.get('ajexq')) else None,'_row_hash':price['_row_hash'],'_batch_id':price['_batch_id'],'_source_row':int(price['_source_row'])}
            result=calculate(transformed,as_of,mode,eps_basis,include_extraordinary,quarterly_lag,annual_lag,max_price_age,(),currency_policy,share_policy,external_price=external)
            if reason:
                result['valuation_status']=reason
                for name in ['pe_ttm','pe_static','pe_annualized','pb','ps_ttm']:result['metrics'][name].update(value=None,reason=reason)
                result['estimated_market_cap_millions']=None
            exact=matches[0]['cusip'][:8]==str(price.get('CUSIP') or price.get('CUSIP9') or '')[:8]
            result.update(metadata);result.update(gvkey=gvkey,matching_status='manual_identity_price_supported' if manual and not reason else 'exact_cusip_price_supported' if exact and not reason else 'inferred_historical_price_supported' if not reason else 'candidate_unit_unverified',matching_reason=reason,financial_period=str(latest['datadate']),release_date=str(availability(latest,quarterly_lag,annual_lag)[0]),release_date_source=availability(latest,quarterly_lag,annual_lag)[1],release_effective_date=effective(latest)[0] if effective(latest)[0]!='9999-12-31' else None,split_multiplier_since_financial_period=split,mode=mode)
            result['validation_anchor_period']=str(validation['datadate'])
            if latest.get('_source')=='sec':result['warnings'].append('sec_financials_and_supplier_price_validation_anchor_are_separate')
            result['warnings']+=['company_mapping_inferred_not_official_CCM','quarter_price_comparison_tolerance_0.5_percent','daily_currency_does_not_certify_all_financial_inputs']
            return json_safe(result)
    def metrics_series(self,security,start=None,end=None,limit=100,offset=0,generation=None,financial_generation=None,**kwargs):
        limits(limit,offset)
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        source=source_choice(kwargs.get('data_source'),financial_generation,kwargs.get('batch_id'),kwargs.get('sec_generation'))
        sm=self.financial._pin(source,kwargs.get('sec_generation'),kwargs.get('rule_version'))
        kwargs['data_source']=source
        if source!='compustat':kwargs.update(sec_generation=sm['sec_generation'] if sm else 'none',rule_version=sm['rule_version'] if sm else 'none')
        with self.store.connect(generation) as (c,m),self.financial.store.connect(financial_generation) as f:
            permno=self._resolve(c,security);rows=self._history(c,permno,end)
            dates=[r['DlyCalDt'] for r in rows if not start or r['DlyCalDt']>=str(start)]
            context={'market_connection':c,'manifest':m,'financial_connection':f,'inputs':{},'calendar':[r[0] for r in c.execute('SELECT DISTINCT DlyCalDt FROM daily ORDER BY DlyCalDt').fetchall()]}
            results=[self.daily_metrics(permno,day,_context=context,**kwargs) for day in dates[offset:offset+limit]]
            return {'market_generation':m['generation'],'database_generation':generation_of(f),'scope':m['scope'],'permno':permno,'total':len(dates),'results':results}
    def screen(self,as_of,filters=None,security_types=None,limit=100,offset=0,generation=None,financial_generation=None,data_source=None,sec_generation=None,rule_version=None):
        source=source_choice(data_source,financial_generation,None,sec_generation);sm=self.financial._pin(source,sec_generation,rule_version)
        sec_args=dict(data_source=source,sec_generation=sm['sec_generation'] if sm else 'none',rule_version=sm['rule_version'] if sm else 'none') if source!='compustat' else dict(data_source=source)
        limits(limit,offset);filters=filters or []
        allowed=METRICS|{'sma_20','sma_60','sma_120','sma_250','rsi','atr','macd','macd_signal','macd_histogram','bb_middle','bb_upper','bb_lower','momentum_21','momentum_63','momentum_126','momentum_252','total_return_21','volatility_21'}
        if len(filters)>20 or any(set(x)!={'metric','op','value'} or x['metric'] not in allowed or x['op'] not in OPS or not math.isfinite(x['value']) for x in filters):raise ValueError('invalid_market_filter')
        need_finance=any(x['metric'] in METRICS for x in filters);found=[]
        with self.store.connect(generation) as (c,m):
            # Pin both sides for the entire screen, including per-security calls.
            fg=financial_generation
            if fg is None:
                try:fg=self.financial.store.active_path().name
                except ValueError:
                    if need_finance:raise
            points=rows_from(c.execute('SELECT PERMNO,SecurityType,SecuritySubType,Ticker,ShareType,DlyCalDt,count(*) OVER(PARTITION BY PERMNO) AS history_rows FROM daily WHERE DlyCalDt<=? QUALIFY row_number() OVER(PARTITION BY PERMNO ORDER BY DlyCalDt DESC)=1',[str(as_of)]))
            types=security_types or ['COM']
            if any(t not in {'COM','ETF','CEF','ALL'} for t in types):raise ValueError('unknown_security_type')
            points=[r for r in points if ('ALL' in types or r['SecuritySubType'] in types) and (('COM' not in types or 'ALL' in types or r['SecuritySubType']!='COM') or (r['SecurityType']=='EQTY' and r['ShareType'] in {'NS','AD'})) and (date(as_of)-date(r['DlyCalDt'])).days<=5]
            required=1
            for item in filters:
                key=item['metric']
                if key.startswith(('sma_','momentum_','total_return_','volatility_')):required=max(required,int(key.rsplit('_',1)[1])+1)
                elif key in {'rsi','atr'}:required=max(required,15)
                elif key.startswith('macd'):required=max(required,35)
                elif key.startswith('bb_'):required=max(required,20)
            if filters and all(x['metric'] in {'pe_ttm','pe_static','pe_annualized','pb','ps_ttm'} for x in filters):required=max(required,2)
            for point in points:
                if point['history_rows']<required:continue
                row={'permno':point['PERMNO'],'ticker':point['Ticker'],'price_date':point['DlyCalDt'],'values':{},'reasons':{}}
                if any(x["metric"] not in METRICS for x in filters):
                    tech=self.technical(point['PERMNO'],start=point['DlyCalDt'],end=as_of,generation=m['generation'],limit=1)
                    if tech['rows']:row['values'].update(tech['rows'][-1]['values']);row['reasons'].update(tech['rows'][-1]['unavailable'])
                if need_finance:
                    metrics=self.daily_metrics(point['PERMNO'],as_of,generation=m['generation'],financial_generation=fg,**sec_args)
                    row['matching_status']=metrics['matching_status'];row['values'].update({k:v['value'] for k,v in metrics['metrics'].items()});row['reasons'].update({k:v.get('reason') for k,v in metrics['metrics'].items() if v['value'] is None})
                if all(row['values'].get(x['metric']) is not None and OPS[x['op']](row['values'][x['metric']],x['value']) for x in filters):found.append(row)
            found.sort(key=lambda r:r['permno'])
            return {'market_generation':m['generation'],'database_generation':fg,'sec_generation':sm['sec_generation'] if sm else None,'rule_version':sm['rule_version'] if sm else None,'data_source':source,'scope':m['scope'],'as_of':str(as_of),'total':len(found),'rows':found[offset:offset+limit],'warnings':['sample_screen_incomplete_history'] if m['scope']!='full' else []}
    def export(self,securities=None,start=None,end=None,as_of=None,filters=None,format='csv',max_rows=100000,generation=None,financial_generation=None,dataset='prices',security_types=None,basis=None,adjustment='forward',purpose='general',data_source=None,sec_generation=None,rule_version=None):
        source=source_choice(data_source,financial_generation,None,sec_generation);sm=self.financial._pin(source,sec_generation,rule_version)
        sec_args=dict(data_source=source,sec_generation=sm['sec_generation'] if sm else 'none',rule_version=sm['rule_version'] if sm else 'none') if source!='compustat' else dict(data_source=source)
        if format not in {'csv','parquet'} or not 1<=max_rows<=100000:raise ValueError('invalid_export_options')
        if start and end and str(start)>str(end):raise ValueError('start_must_not_follow_end')
        if dataset not in {'prices','adjusted_prices','technical','daily_metrics'}:raise ValueError('unknown_export_dataset')
        if dataset in {'adjusted_prices','technical','daily_metrics'} and not securities:raise ValueError('technical_export_requires_security_pool')
        if dataset in {'adjusted_prices','technical'}:
            effective_purpose='technical' if dataset=='technical' else purpose
            effective_basis=resolve_basis(basis,effective_purpose)
            if adjustment not in {'forward','backward'}:raise ValueError('invalid_adjustment_direction')
            if dataset=='technical' and adjustment!='forward':raise ValueError('technical_export_uses_forward_adjustment')
        elif basis is not None or adjustment!='forward' or purpose!='general':raise ValueError('adjustment_options_require_adjusted_prices_or_technical')
        if securities and as_of:raise ValueError('choose_securities_range_or_screen_date')
        if not securities and not as_of:raise ValueError('export_requires_securities_or_as_of')
        folder=self.financial.store.root/'exports';folder.mkdir(parents=True,exist_ok=True);eid=uuid.uuid4().hex;dest=folder/f'{eid}.{format}'
        with self.store.connect(generation) as (c,m):
            if dataset=='daily_metrics':financial_generation=financial_generation or self.financial.store.active_path().name
            if securities:
                if len(securities)>1000:raise ValueError('security_pool_too_large')
                keys=sorted({self._resolve(c,s) for s in securities});where=['PERMNO IN ('+','.join('?' for _ in keys)+')'];args=list(keys)
                if start:where.append('DlyCalDt>=?');args.append(str(start))
                if end:where.append('DlyCalDt<=?');args.append(str(end))
                query='SELECT * FROM daily WHERE '+' AND '.join(where)+' ORDER BY PERMNO,DlyCalDt'
                total=c.execute('SELECT count(*) FROM ('+query+')',args).fetchone()[0]
                if dataset=='prices':
                    c.execute('COPY ('+query+' LIMIT '+str(max_rows)+') TO '+literal(dest)+' ('+('FORMAT PARQUET' if format=='parquet' else 'FORMAT CSV,HEADER true')+')',args)
                else:
                    self._export_derived(dest,format,dataset,keys,start,end,max_rows,m['generation'],financial_generation,basis,adjustment,purpose,sec_args)
                metadata={'market_generation':m['generation'],'scope':m['scope'],'total':total,'exported_rows':min(total,max_rows),'truncated':total>max_rows,'securities':keys,'start':str(start) if start else None,'end':str(end) if end else None,'kind':dataset}
            else:
                first=self.screen(as_of,filters,security_types=security_types,limit=1000,generation=m['generation'],financial_generation=financial_generation,**sec_args);data=first['rows']
                for offset in range(1000,min(max_rows,first['total']),1000):data+=self.screen(as_of,filters,security_types=security_types,limit=min(1000,max_rows-offset),offset=offset,generation=m['generation'],financial_generation=first['database_generation'],**sec_args)['rows']
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    p=Path(tmp)/'screen.json';p.write_text(json.dumps(data))
                    if data and not filters:
                        snapshot_keys=[r['permno'] for r in data]
                        snapshot_sql='SELECT * FROM daily WHERE PERMNO IN ('+','.join('?' for _ in snapshot_keys)+') AND DlyCalDt<=? QUALIFY row_number() OVER(PARTITION BY PERMNO ORDER BY DlyCalDt DESC)=1 ORDER BY PERMNO'
                        c.execute('COPY ('+snapshot_sql+') TO '+literal(dest)+' ('+('FORMAT PARQUET' if format=='parquet' else 'FORMAT CSV,HEADER true')+')',snapshot_keys+[str(as_of)])
                    elif data:c.execute('COPY (SELECT * FROM read_json_auto('+literal(p)+')) TO '+literal(dest)+' ('+('FORMAT PARQUET' if format=='parquet' else 'FORMAT CSV,HEADER true')+')')
                    elif format=='csv':dest.write_text('permno,ticker,price_date\n')
                    else:c.execute("COPY (SELECT NULL::VARCHAR AS permno WHERE false) TO ? (FORMAT PARQUET)",[str(dest)])
                metadata={k:v for k,v in first.items() if k!='rows'};metadata.update(exported_rows=len(data),truncated=first['total']>max_rows,kind='screen')
        if dataset in {'adjusted_prices','technical'}:metadata.update(basis=effective_basis,basis_source='explicit' if basis is not None else 'purpose_default',adjustment=adjustment,purpose=effective_purpose,ordinary_cash_dividends_included=effective_basis=='total_return',adjustment_policy_version=ADJUSTMENT_POLICY)
        if dataset=='daily_metrics':metadata.update(database_generation=financial_generation,sec_generation=sm['sec_generation'] if sm else None,rule_version=sm['rule_version'] if sm else None,data_source=source)
        (folder/f'{eid}.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
        return {'export_id':eid,'format':format,'path':str(dest),'metadata':metadata}

    def _export_derived(self,dest,format,dataset,keys,start,end,max_rows,generation,financial_generation,basis=None,adjustment='forward',purpose='general',sec_args=None):
        import csv
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            staged=Path(tmp)/'derived.csv';writer=None;remaining=max_rows
            with staged.open('w',newline='') as out:
                for key in keys:
                    offset=0
                    while remaining>0:
                        params=dict(security=key,start=start,end=end,limit=min(1000,remaining),offset=offset,generation=generation)
                        if dataset=='adjusted_prices':response=self.adjusted_prices(**params,basis=basis,adjustment=adjustment,purpose=purpose)
                        elif dataset=='technical':response=self.technical(**params,basis=resolve_basis(basis,'technical'))
                        else:response=self.metrics_series(**params,financial_generation=financial_generation,**(sec_args or {}))
                        rows=response['rows' if dataset in {'adjusted_prices','technical'} else 'results']
                        if not rows:break
                        for point in rows:
                            if dataset=='adjusted_prices':
                                flat={k:v for k,v in point.items() if k!='unavailable'};flat['unavailable']=json.dumps(point['unavailable'],ensure_ascii=False);flat['market_generation']=generation
                            elif dataset=='technical':
                                flat={k:point[k] for k in ['permno','date','raw_price','adjusted_price','anchor_date']}
                                flat.update(point['values']);flat.update(unavailable=json.dumps(point['unavailable'],ensure_ascii=False),market_generation=generation,basis=resolve_basis(basis,'technical'),adjustment='forward')
                            else:
                                flat={'permno':key,'date':point.get('as_of') or point.get('price_date'),'gvkey':point.get('gvkey'),'price':point.get('price'),'price_date':point.get('price_date'),'financial_period':point.get('financial_period'),'release_date':point.get('release_date'),'release_date_source':point.get('release_date_source'),'release_effective_date':point.get('release_effective_date'),'matching_status':point.get('matching_status'),'price_currency':point.get('price_currency'),'market_generation':generation,'database_generation':financial_generation}
                                flat.update(data_source=point.get('data_source'),sec_generation=point.get('sec_generation'),rule_version=point.get('rule_version'),input_facts=json.dumps({k:v.get('input_facts',[]) for k,v in point['metrics'].items()},ensure_ascii=False),warnings=json.dumps(point.get('warnings',[]),ensure_ascii=False))
                                flat.update({name:point['metrics'].get(name,{}).get('value') for name in sorted(METRICS)})
                                flat['unavailable']=json.dumps({name:value.get('reason') for name,value in point['metrics'].items() if value['value'] is None},ensure_ascii=False)
                            if writer is None:writer=csv.DictWriter(out,fieldnames=list(flat));writer.writeheader()
                            writer.writerow(flat);remaining-=1
                        offset+=len(rows)
                        if offset>=response['total']:break
                    if remaining==0:break
                if writer is None:out.write('permno,date\n')
            if format=='csv':
                import shutil
                shutil.copyfile(staged,dest)
            else:
                with duckdb.connect() as c:
                    c.execute('COPY (SELECT * FROM read_csv('+literal(staged)+",header=true,types={'permno':'VARCHAR','date':'VARCHAR'})) TO "+literal(dest)+' (FORMAT PARQUET)')
