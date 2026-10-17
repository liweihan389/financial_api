from typing import Literal, Annotated, Any
import datetime as dt
from pathlib import Path
from fastapi import APIRouter,Request,Query
from fastapi.responses import HTMLResponse,FileResponse
from pydantic import BaseModel,ConfigDict,Field,model_validator
from mcp.types import ToolAnnotations

class MarketPage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    generation:str|None=None
    limit:int=Field(default=100,ge=1,le=1000)
    offset:int=Field(default=0,ge=0)
class SecuritySearch(MarketPage):
    query:str=''
    as_of:dt.date|None=None
class Prices(MarketPage):
    security:str=Field(min_length=1,max_length=100)
    start:dt.date|None=None
    end:dt.date|None=None
    fields:list[str]|None=None
    events:bool=False
    @model_validator(mode='after')
    def dates(self):
        if self.start and self.end and self.start>self.end:raise ValueError('start_must_not_follow_end')
        return self
class AdjustedPrices(Prices):
    fields:None=None
    events:Literal[False]=False
    basis:Literal['price_return','total_return']|None=None
    adjustment:Literal['forward','backward']='forward'
    purpose:Literal['general','technical']='general'
class Technical(Prices):
    fields:None=None
    events:Literal[False]=False
    sma:list[int]=Field(default_factory=lambda:[20,60,120,250],max_length=12)
    ema_periods:list[int]=Field(default_factory=lambda:[12,26],max_length=12)
    rsi_period:int=Field(default=14,ge=1,le=5000)
    atr_period:int=Field(default=14,ge=1,le=5000)
    bb_period:int=Field(default=20,ge=2,le=5000)
    bb_std:float=Field(default=2,gt=0,le=10)
    macd:tuple[int,int,int]=(12,26,9)
    windows:list[int]=Field(default_factory=lambda:[21,63,126,252],max_length=12)
    annual_days:int=Field(default=252,ge=1,le=1000)
    basis:Literal['price_return','total_return']='price_return'
class Returns(BaseModel):
    model_config=ConfigDict(extra='forbid')
    security:str
    start:dt.date|None=None
    end:dt.date|None=None
    generation:str|None=None
    basis:Literal['price_return','total_return']='total_return'
    annual_days:int=Field(default=252,ge=1,le=1000)
class DailyMetrics(BaseModel):
    data_source:Literal['combined','compustat','sec']|None=None
    sec_generation:str|None=None
    rule_version:str|None=None
    model_config=ConfigDict(extra='forbid')
    security:str
    as_of:dt.date|None=None
    start:dt.date|None=None
    end:dt.date|None=None
    limit:int=Field(default=100,ge=1,le=1000)
    offset:int=Field(default=0,ge=0)
    @model_validator(mode='after')
    def date_options(self):
        if self.as_of and (self.start or self.end):raise ValueError('choose_as_of_or_date_range')
        if not self.as_of and not (self.start and self.end):raise ValueError('choose_as_of_or_complete_date_range')
        if self.start and self.end and self.start>self.end:raise ValueError('start_must_not_follow_end')
        return self
    mode:Literal['research','historical_approx','strict']='historical_approx'
    generation:str|None=None
    financial_generation:str|None=None
    batch_id:str|None=None
    eps_basis:Literal['diluted','basic']='diluted'
    include_extraordinary:bool=True
    quarterly_lag:int=Field(default=90,ge=0,le=365)
    annual_lag:int=Field(default=120,ge=0,le=365)
    max_price_age:int=Field(default=5,ge=0,le=365)
    currency_policy:Literal['infer_curcdq','verified_only']='infer_curcdq'
    share_policy:Literal['infer_compustat','verified_only']='infer_compustat'
class MarketFilter(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    metric:str
    op:Literal['eq','ne','gt','gte','lt','lte']
    value:float
class MarketScreen(MarketPage):
    data_source:Literal['combined','compustat','sec']|None=None
    sec_generation:str|None=None
    rule_version:str|None=None

    as_of:dt.date
    financial_generation:str|None=None
    filters:list[MarketFilter]=Field(default_factory=list,max_length=20)
    security_types:list[Literal['COM','ETF','CEF','ALL']]|None=None
class MarketExport(BaseModel):
    data_source:Literal['combined','compustat','sec']|None=None
    sec_generation:str|None=None
    rule_version:str|None=None

    dataset:Literal['prices','adjusted_prices','technical','daily_metrics']='prices'
    basis:Literal['price_return','total_return']|None=None
    adjustment:Literal['forward','backward']='forward'
    purpose:Literal['general','technical']='general'
    security_types:list[Literal['COM','ETF','CEF','ALL']]|None=None
    model_config=ConfigDict(extra='forbid')
    securities:list[str]|None=Field(default=None,max_length=1000)
    start:dt.date|None=None
    end:dt.date|None=None
    as_of:dt.date|None=None
    filters:list[MarketFilter]|None=Field(default=None,max_length=20)
    format:Literal['csv','parquet']='csv'
    max_rows:int=Field(default=100000,ge=1,le=100000)
    generation:str|None=None
    financial_generation:str|None=None
class Upload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    size:int=Field(gt=0)
class Finish(BaseModel):
    sha256:str|None=None
class Import(BaseModel):
    model_config=ConfigDict(extra='forbid')
    upload_id:str
    kind:Literal['market','financial','links','sec']='market'
    scope:Literal['sample','full']='sample'

def register(app,mcp,market,admin,public_url,trace):
    router=APIRouter();read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    def adjusted(request):
        args=request.model_dump();args.pop('fields');args.pop('events');return market.adjusted_prices(**args)
    def technical(request):
        args=request.model_dump();args.pop('fields');args.pop('events');return market.technical(**args)
    def daily_metrics(request):
        args=request.model_dump()
        if args['as_of'] is None:
            args.pop('as_of');return market.metrics_series(**args)
        for k in ['start','end','limit','offset']:args.pop(k)
        return market.daily_metrics(**args)
    def export(request):
        result=market.export(**request.model_dump());result['download_url']=public_url+'/v1/exports/'+result['export_id'];return result
    @mcp.tool(annotations=read)
    @trace
    def get_market_data_status(generation:str|None=None)->dict[str,Any]:
        """Daily CRSP sample/full coverage, immutable generations, source batches and quality counts."""
        return market.status(generation)
    @mcp.tool(annotations=read)
    @trace
    def search_securities(request:SecuritySearch)->dict[str,Any]:
        """Search historical security identities. Use PERMNO to disambiguate share classes and reused tickers."""
        return market.search(**request.model_dump())
    @mcp.tool(annotations=read)
    @trace
    def get_daily_prices(request:Prices)->dict[str,Any]:
        """Raw daily prices or separate distribution events, with source-row provenance. Not realtime."""
        return market.prices(**request.model_dump())
    @mcp.tool(annotations=read)
    @trace
    def get_adjusted_prices(request:AdjustedPrices)->dict[str,Any]:
        """Forward/backward adjusted OHLC. General charts default dividend-inclusive total_return; purpose=technical defaults price_return excluding ordinary dividends. Explicit basis overrides defaults. Anchors use the requested range, never pagination."""
        return adjusted(request)
    @mcp.tool(annotations=read)
    @trace
    def get_daily_metrics(request:DailyMetrics)->dict[str,Any]:
        """Daily PE/PB/PS after CUSIP, period-price and per-share unit checks. Publication effective next observed trading day; approximate history."""
        return daily_metrics(request)
    @mcp.tool(annotations=read)
    @trace
    def get_technical_indicators(request:Technical)->dict[str,Any]:
        """SMA/EMA/RSI/MACD/Bollinger/ATR/momentum. Price-return adjustment, query-end anchoring and explicit warmup defaults."""
        return technical(request)
    @mcp.tool(annotations=read)
    @trace
    def get_return_statistics(request:Returns)->dict[str,Any]:
        """Cumulative return, annualized sample volatility and maximum drawdown; total returns by default."""
        return market.returns(**request.model_dump())
    @mcp.tool(annotations=read)
    @trace
    def screen_securities(request:MarketScreen)->dict[str,Any]:
        """Single-date common-stock/ADR screening with controlled financial and technical filters; sample coverage explicitly marked."""
        return market.screen(**request.model_dump())
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False))
    @trace
    def export_market_dataset(request:MarketExport)->dict[str,Any]:
        """Export raw/adjusted prices, technical or daily financial panels. Adjusted OHLC defaults dividend-inclusive in both directions; technical defaults exclude ordinary dividends. Explicit basis overrides defaults."""
        return export(request)
    @router.get('/ui',response_class=HTMLResponse)
    @router.get('/',response_class=HTMLResponse)
    def ui():return (Path(__file__).parent/'static'/'index.html').read_text()
    @router.get('/v1/market/status')
    def status(generation:str|None=None):return market.status(generation)
    @router.get('/v1/market/securities')
    def securities(request:Annotated[SecuritySearch,Query()]):return market.search(**request.model_dump())
    @router.post('/v1/market/prices')
    def prices(request:Prices):return market.prices(**request.model_dump())
    @router.post('/v1/market/adjusted-prices')
    def adjusted_prices(request:AdjustedPrices):return adjusted(request)
    @router.post('/v1/market/metrics')
    def metrics(request:DailyMetrics):return daily_metrics(request)
    @router.post('/v1/market/technical')
    def tech(request:Technical):return technical(request)
    @router.post('/v1/market/returns')
    def returns(request:Returns):return market.returns(**request.model_dump())
    @router.post('/v1/market/screen')
    def screen(request:MarketScreen):return market.screen(**request.model_dump())
    @router.post('/v1/market/export')
    def exporting(request:MarketExport):return export(request)
    @router.get('/v1/admin/history')
    def history(limit:int=100,offset:int=0,query:str=''):return admin.history(limit,offset,query)
    @router.get('/v1/admin/uploads')
    def uploads():return admin.uploads()
    @router.post('/v1/admin/uploads')
    def start_upload(request:Upload):return admin.create_upload(request.name,request.size)
    @router.get('/v1/admin/uploads/{uid}')
    def upload(uid:str):return admin.upload(uid)
    @router.put('/v1/admin/uploads/{uid}/chunks')
    async def chunk(uid:str,offset:int,request:Request):return await admin.append(uid,offset,request)
    @router.post('/v1/admin/uploads/{uid}/finish')
    def finish(uid:str,request:Finish):return admin.finish(uid,request.sha256)
    @router.post('/v1/admin/imports')
    def imports(request:Import):return admin.start_job(request.upload_id,request.kind,request.scope)
    @router.get('/v1/admin/imports')
    def jobs():return admin.jobs()
    app.include_router(router)
