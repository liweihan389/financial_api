"""Purpose-aware dividend defaults and independently anchored OHLC adjustment."""
import math
from .metrics import number
from .technical import aligned,valid_return

POLICY='dividend-adjustment-v1'

def resolve_basis(basis=None,purpose='general'):
    if purpose not in {'general','technical'}:raise ValueError('invalid_adjustment_purpose')
    value=basis if basis is not None else 'price_return' if purpose=='technical' else 'total_return'
    if value not in {'price_return','total_return'}:raise ValueError('invalid_price_basis')
    return value

def backward_aligned(rows,field):
    prices=[];anchors=[];previous=None;anchor=None
    for i,row in enumerate(rows):
        raw=number(row.get('DlyPrc'))
        if raw is None or raw<=0 or row.get('_quote_conflict'):
            prices.append(None);anchors.append(None);previous=None;anchor=None;continue
        r=valid_return(row,field)
        consecutive=i>0 and str(row.get('DlyPrevDt') or '')==str(rows[i-1]['DlyCalDt'])
        if previous is not None and consecutive and r is not None and r>-1:
            value=previous*(1+r)
            if not math.isfinite(value) or value<=0:value=raw;anchor=str(row['DlyCalDt'])
        else:value=raw;anchor=str(row['DlyCalDt'])
        prices.append(value);anchors.append(anchor);previous=value
    return prices,anchors

def adjusted_ohlc(rows,basis=None,adjustment='forward',purpose='general'):
    basis=resolve_basis(basis,purpose)
    if adjustment not in {'forward','backward'}:raise ValueError('invalid_adjustment_direction')
    field='DlyRet' if basis=='total_return' else 'DlyRetx'
    prices,anchors=aligned(rows,field) if adjustment=='forward' else backward_aligned(rows,field)
    output=[]
    for row,price,anchor in zip(rows,prices,anchors):
        raw=number(row.get('DlyPrc'));factor=price/raw if price is not None and raw and raw>0 else None
        point={'permno':row['PERMNO'],'date':str(row['DlyCalDt']),'raw_price':raw,'adjusted_price':price,'raw_close':raw,'adjusted_close':price,'raw_volume':number(row.get('DlyVol')),'adjustment_factor':factor,'anchor_date':anchor,'price_flag':row.get('DlyPrcFlg'),'basis':basis,'adjustment':adjustment,'purpose':purpose,'_batch_id':row.get('_batch_id'),'_source_row':row.get('_source_row'),'unavailable':{}}
        for name in ['open','high','low']:
            value=number(row.get('Dly'+name.title()));point['raw_'+name]=value
            point['adjusted_'+name]=value*factor if value is not None and factor is not None else None
            if point['adjusted_'+name] is None:point['unavailable'][name]='missing_raw_price_or_adjustment'
        if price is None:point['unavailable']['close']='invalid_or_conflicting_price'
        output.append(point)
    return output
