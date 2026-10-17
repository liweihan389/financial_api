"""Dependency-free, explicit CRSP price-return and total-return calculations."""
import math
import statistics
from collections import deque
from .metrics import number

def valid_return(row,field):
    value=number(row.get(field))
    if row.get('_quote_conflict') or row.get('DlyRetMissFlg') not in {None,'','NA'} or str(row.get('DlyRetDurFlg','')).startswith(('P','MR')):
        return None
    return value if value is not None and value>=-1 else None

def aligned(rows,field='DlyRetx'):
    """Each uninterrupted segment is anchored to its final raw price.
    Never bridge a missing/multiple-period return or absent prior observation.
    """
    out=[None]*len(rows);anchors=[None]*len(rows);end=len(rows)-1
    while end>=0:
        value=number(rows[end].get('DlyPrc'))
        if value is None or value<=0 or rows[end].get('_quote_conflict'):
            end-=1;continue
        out[end]=value;anchors[end]=rows[end]['DlyCalDt'];i=end
        while i>0:
            r=valid_return(rows[i],field)
            prior=str(rows[i].get('DlyPrevDt') or '')
            prior_price=number(rows[i-1].get('DlyPrc'))
            if r is None or r<=-1 or prior!=str(rows[i-1]['DlyCalDt']) or rows[i-1].get('_quote_conflict') or prior_price is None or prior_price<=0:break
            value=value/(1+r)
            if not math.isfinite(value):break
            out[i-1]=value;anchors[i-1]=rows[end]['DlyCalDt'];i-=1
        end=i-1
    return out,anchors

def rolling(values,n,fn):
    result=[];q=deque()
    for v in values:
        q.append(v)
        if len(q)>n:q.popleft()
        result.append(fn(list(q)) if len(q)==n and all(x is not None for x in q) else None)
    return result

def ema(values,n):
    result=[];q=[];previous=None
    for v in values:
        if v is None:previous=None;q=[];result.append(None);continue
        if previous is None:
            q.append(v)
            if len(q)==n:previous=sum(q)/n
        else:previous=previous+(2/(n+1))*(v-previous)
        result.append(previous)
    return result

def wilder(values,n):
    result=[];q=[];previous=None
    for v in values:
        if v is None:previous=None;q=[];result.append(None);continue
        if previous is None:
            q.append(v)
            if len(q)==n:previous=sum(q)/n
        else:previous=(previous*(n-1)+v)/n
        result.append(previous)
    return result

def indicators(rows,sma=(20,60,120,250),ema_periods=(12,26),rsi_period=14,atr_period=14,bb_period=20,bb_std=2,macd=(12,26,9),windows=(21,63,126,252),annual_days=252,basis='price_return'):
    if basis not in {'price_return','total_return'}:raise ValueError('invalid_price_basis')
    periods=list(sma)+list(ema_periods)+[rsi_period,atr_period,bb_period]+list(macd)+list(windows)
    if any(not isinstance(n,int) or not 1<=n<=5000 for n in periods) or len(periods)>40 or not 1<=annual_days<=1000 or not 0<bb_std<=10:raise ValueError('invalid_indicator_parameters')
    p,anchors=aligned(rows,'DlyRetx' if basis=='price_return' else 'DlyRet')
    from itertools import groupby
    segments=[list(group) for _,group in groupby(range(len(rows)),key=lambda i:anchors[i])]
    if len(segments)>1:
        output=[]
        for segment in segments:
            output+=indicators([rows[i] for i in segment],sma,ema_periods,rsi_period,atr_period,bb_period,bb_std,macd,windows,annual_days,basis)
        return output
    series={}
    for n in sma:series[f'sma_{n}']=rolling(p,n,statistics.fmean)
    for n in ema_periods:series[f'ema_{n}']=ema(p,n)
    changes=[None]+[p[i]-p[i-1] if p[i] is not None and p[i-1] is not None else None for i in range(1,len(p))]
    up=wilder([max(0,v) if v is not None else None for v in changes],rsi_period)
    down=wilder([max(0,-v) if v is not None else None for v in changes],rsi_period)
    series['rsi']=[None if a is None or b is None else 50 if a==b==0 else 100 if b==0 else 100-100/(1+a/b) for a,b in zip(up,down)]
    ef,es=ema(p,macd[0]),ema(p,macd[1]);m=[a-b if a is not None and b is not None else None for a,b in zip(ef,es)]
    signal=ema(m,macd[2]);series.update(macd=m,macd_signal=signal,macd_histogram=[a-b if a is not None and b is not None else None for a,b in zip(m,signal)])
    mid=rolling(p,bb_period,statistics.fmean);sd=rolling(p,bb_period,statistics.pstdev)
    series.update(bb_middle=mid,bb_upper=[a+bb_std*b if a is not None and b is not None else None for a,b in zip(mid,sd)],bb_lower=[a-bb_std*b if a is not None and b is not None else None for a,b in zip(mid,sd)])
    tr=[]
    for i,row in enumerate(rows):
        raw=number(row.get('DlyPrc'));high=number(row.get('DlyHigh'));low=number(row.get('DlyLow'))
        scale=p[i]/raw if p[i] is not None and raw and raw>0 else None
        previous=p[i-1] if i and anchors[i]==anchors[i-1] else None
        if high is None or low is None or scale is None:tr.append(None)
        else:
            high*=scale;low*=scale
            tr.append(max(high-low,abs(high-previous),abs(low-previous)) if previous is not None else high-low)
    series['atr']=wilder(tr,atr_period)
    total=[valid_return(r,'DlyRet') for r in rows]
    for n in windows:
        series[f'momentum_{n}']=[p[i]/p[i-n]-1 if i>=n and p[i] is not None and p[i-n] is not None and p[i-n]>0 and anchors[i]==anchors[i-n] else None for i in range(len(p))]
        series[f'total_return_{n}']=rolling(total,n,lambda xs:math.prod(1+x for x in xs)-1)
        series[f'volatility_{n}']=rolling(total,n,lambda xs:statistics.stdev(xs)*math.sqrt(annual_days) if len(xs)>1 else None)
    output=[]
    for i,row in enumerate(rows):
        values={k:v[i] if v[i] is None or math.isfinite(v[i]) else None for k,v in series.items()}
        output.append({'date':str(row['DlyCalDt']),'permno':row['PERMNO'],'raw_price':number(row.get('DlyPrc')),'adjusted_price':p[i], 'anchor_date':anchors[i],'values':values,'unavailable':{k:'insufficient_warmup_or_missing_inputs' for k,v in values.items() if v is None}})
    return output

def return_statistics(rows,basis='total_return',annual_days=252):
    if basis not in {'price_return','total_return'} or not 1<=annual_days<=1000:raise ValueError('invalid_return_parameters')
    field='DlyRet' if basis=='total_return' else 'DlyRetx'
    # Holding from first observed close to last close: first row's return is
    # outside that holding interval and is excluded.
    returns=[];wealth=1;peak=1;max_dd=0;peak_day=str(rows[0]['DlyCalDt']) if rows else None;dd_peak=peak_day;dd_trough=peak_day;bad=False
    for i,row in enumerate(rows[1:],1):
        v=valid_return(row,field)
        if str(row.get('DlyPrevDt') or '')!=str(rows[i-1]['DlyCalDt']):v=None
        if v is None:bad=True;continue
        returns.append(v);wealth*=1+v
        if wealth>peak:peak=wealth;peak_day=str(row['DlyCalDt'])
        dd=wealth/peak-1
        if dd<max_dd:max_dd=dd;dd_peak=peak_day;dd_trough=str(row['DlyCalDt'])
    reason='missing_or_noncontiguous_returns' if bad else 'insufficient_observations' if len(rows)<2 else None
    return {'basis':basis,'formula':'product(1 + source_return) - 1; first_close_to_last_close','observations':len(rows),'valid_return_count':len(returns),'annual_days':annual_days,'cumulative_return':wealth-1 if reason is None else None,'annualized_volatility':statistics.stdev(returns)*math.sqrt(annual_days) if reason is None and len(returns)>1 else None,'maximum_drawdown':max_dd if reason is None else None,'drawdown_peak_date':dd_peak if reason is None else None,'drawdown_trough_date':dd_trough if reason is None else None,'reason':reason}
