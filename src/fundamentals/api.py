from contextlib import asynccontextmanager
from functools import wraps
import time
import datetime as dt
import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal
import uuid

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator
from urllib.parse import urlparse

from .sec_combined import FinancialService

class Paging(BaseModel):
    model_config=ConfigDict(extra="forbid")
    limit:int=Field(default=100,ge=1,le=1000)
    offset:int=Field(default=0,ge=0)
    generation:str|None=None

class DictionaryQuery(Paging):
    source:Literal["financial","market"]="financial"
    query:str|None=None

class CompanyQuery(Paging):
    query:str=""

class Policy(Paging):
    data_source:Literal['combined','compustat','sec']|None=None
    sec_generation:str|None=None
    rule_version:str|None=None
    mode:Literal["research","historical_approx","strict"]="historical_approx"
    batch_id:str|None=None
    quarterly_lag:int=Field(default=90,ge=0,le=365)
    annual_lag:int=Field(default=120,ge=0,le=365)

class FinancialQuery(Policy):
    company:str=Field(min_length=1,max_length=100)
    fields:list[str]|None=None
    start:dt.date|None=None
    end:dt.date|None=None
    as_of:dt.date|None=None
    mode:Literal["research","historical_approx","strict"]="research"

    @model_validator(mode="after")
    def date_range(self):
        if self.start and self.end and self.start>self.end:
            raise ValueError("start_must_not_follow_end")
        return self

class MetricQuery(Policy):
    company:str=Field(min_length=1,max_length=100)
    as_of:dt.date|None=None
    start:dt.date|None=None
    end:dt.date|None=None
    metrics:list[str]|None=None
    eps_basis:Literal["diluted","basic"]="diluted"
    include_extraordinary:bool=True
    max_price_age:int=Field(default=120,ge=0,le=3650)
    currency_policy:Literal["infer_curcdq","verified_only"]="infer_curcdq"
    share_policy:Literal["infer_compustat","verified_only"]="infer_compustat"

    @model_validator(mode="after")
    def date_range(self):
        if self.start and self.end and self.start>self.end:
            raise ValueError("start_must_not_follow_end")
        if self.as_of and (self.start or self.end):
            raise ValueError("choose_as_of_or_date_range")
        return self

class Filter(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    metric:str
    op:Literal["eq","ne","gt","gte","lt","lte"]
    value:float

class ScreenQuery(Policy):
    as_of:dt.date
    filters:list[Filter]=Field(default_factory=list,max_length=20)
    market:Literal["US","CA","ALL"]="US"
    eps_basis:Literal["diluted","basic"]="diluted"
    include_extraordinary:bool=True
    max_price_age:int=Field(default=120,ge=0,le=3650)
    currency_policy:Literal["infer_curcdq","verified_only"]="infer_curcdq"
    share_policy:Literal["infer_compustat","verified_only"]="infer_compustat"

class ExportQuery(ScreenQuery):
    format:Literal["csv","parquet"]="csv"
    max_rows:int=Field(default=10000,ge=1,le=100000)

def create_app(root=None,token=None,public_url=None):
    service=FinancialService(root)
    from .market_service import MarketService
    from .admin import Admin
    from .market_api import register
    market=MarketService(root)
    admin=Admin(service.store.root)
    from .sec_jobs import SecJobs
    from .sec_api import register_sec
    sec_jobs=SecJobs(admin,service.sec)
    def trace_tool(fn):
        @wraps(fn)
        def traced(*args,**kwargs):
            started=time.monotonic();status=200;result=None
            try:
                result=fn(*args,**kwargs);return result
            except Exception:
                status=400;raise
            finally:
                parameters={k:v.model_dump() if hasattr(v,"model_dump") else v for k,v in kwargs.items()}
                output=result or {};metadata=output.get("metadata",{});versions={}
                for kind,key in [("market_generation","market_generation"),("financial_generation","database_generation"),("sec_generation","sec_generation")]:
                    value=output.get(key) or metadata.get(key)
                    if value:versions[kind]=value
                admin.log("MCP",fn.__name__,parameters,status,time.monotonic()-started,json.dumps(versions) if versions else None)
        return traced
    token=token if token is not None else os.environ.get("FINANCIAL_API_TOKEN")
    public_url=(public_url or os.environ.get("FINANCIAL_PUBLIC_URL","http://127.0.0.1:8765")).rstrip("/")
    public=urlparse(public_url)
    if public.scheme not in {"http","https"} or not public.hostname or public.username or public.password:
        raise ValueError("invalid_public_service_url")
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=["127.0.0.1:*","localhost:*","[::1]:*",public.netloc],allowed_origins=["http://127.0.0.1:*","http://localhost:*",f"{public.scheme}://{public.netloc}"])
    mcp=FastMCP("market-fundamentals",instructions="CRSP daily prices and technical/risk tools complement Compustat quarterly snapshots. Check get_market_data_status: sample/mixed coverage is incomplete. Use PERMNO for securities and GVKEY for companies. Daily valuation requires graded CUSIP/period-price/unit checks; ambiguous mappings return null. Financial disclosure is effective next observed trading day. General forward/backward adjusted OHLC defaults dividend-inclusive total_return. Technical charts/analysis default price_return excluding ordinary cash dividends. Explicit basis always overrides defaults; risk returns default total returns. SEC supplements eligible periods after 2026-02-28; combined is default except explicit legacy generation/batch requests. SEC inferred fields are research-only, historical selection filters visible filings before revisions. No new prices or strict point-in-time certification. SEC network sync requires SEC_USER_AGENT. Compustat revised quarterly snapshots. Historical results are approximate, not strict point-in-time. Currency defaults to infer_curcdq: PRCCQ is assumed to use the same row's CURCDQ, with provenance. verified_only requires independent currency evidence. Share units default to infer_compustat: ADR EPS and shares are assumed already ADS-equivalent; never reapply ADRRQ. share_policy=verified_only requires audited ADR mappings. Known half-year EPS and mixed share classes are restricted; numerical discrepancies remain flagged. Preserve currency and share-basis warnings.",stateless_http=True,json_response=True,transport_security=transport_security)
    read_annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def get_data_status(generation:str|None=None)->dict[str,Any]:
        """Get dataset coverage, import generations and security verification status."""
        return {**service.get_data_status(generation),"market":market.status()}

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def search_companies(request:CompanyQuery)->dict[str,Any]:
        """Find company GVKEY identifiers; tickers can be ambiguous and current."""
        return service.search_companies(**request.model_dump())

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def get_field_dictionary(request:DictionaryQuery)->dict[str,Any]:
        """Find source field descriptions, types, units and PDF references."""
        args=request.model_dump();source=args.pop("source")
        return market.dictionary(**args) if source=="market" else service.get_field_dictionary(**args)

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def get_financials(request:FinancialQuery)->dict[str,Any]:
        """Read quarterly source fields with batch and logical CSV row provenance."""
        return service.get_financials(**request.model_dump())

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def get_metrics(request:MetricQuery)->dict[str,Any]:
        """Calculate financial and valuation ratios. Null values include reasons. Defaults to latest stored company period, not today's price."""
        return service.get_metrics(**request.model_dump())

    @mcp.tool(annotations=read_annotations)
    @trace_tool
    def screen_companies(request:ScreenQuery)->dict[str,Any]:
        """Screen companies on historical quarterly inputs using controlled numeric filters."""
        return service.screen_companies(**request.model_dump())

    def save_export(query):
        content,metadata=service.export_dataset(**query.model_dump())
        folder=service.store.root/"exports"
        folder.mkdir(parents=True,exist_ok=True)
        export_id=uuid.uuid4().hex
        path=folder/f"{export_id}.{query.format}"
        path.write_bytes(content)
        (folder/f"{export_id}.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
        return {"export_id":export_id,"format":query.format,"path":str(path),"download_url":f"{public_url}/v1/exports/{export_id}","metadata":metadata}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False))
    @trace_tool
    def export_dataset(request:ExportQuery)->dict[str,Any]:
        """Create a CSV/Parquet artifact without changing financial data. Returns a local file and download URL with reproducibility metadata."""
        return save_export(request)

    @asynccontextmanager
    async def lifespan(app):
        sec_jobs.start_scheduler()
        try:
            async with mcp.session_manager.run():
                yield
        finally:sec_jobs.stop_scheduler()
        admin.pool.shutdown(wait=False)

    app=FastAPI(title="Market Fundamentals",version="0.3.1",lifespan=lifespan)
    app.state.service=service
    app.state.mcp=mcp
    app.state.market=market
    app.state.admin=admin
    register(app,mcp,market,admin,public_url,trace_tool)
    register_sec(app,mcp,service,sec_jobs,admin,public_url,trace_tool)

    @app.middleware("http")
    async def authenticate(request:Request,call_next):
        path=request.url.path
        host=urlparse('http://'+request.headers.get('host','')).hostname
        if host not in {'127.0.0.1','localhost','::1','testserver',public.hostname}:
            return JSONResponse({'error':'untrusted_host'},status_code=400)
        origin=request.headers.get('origin')
        if origin and path.startswith('/v1/') and request.method not in {'GET','HEAD'}:
            expected={f'{request.url.scheme}://{request.headers.get("host","")}',f'{public.scheme}://{public.netloc}'}
            if origin not in expected:return JSONResponse({'error':'untrusted_origin'},status_code=403)
        if token and path not in {"/healthz","/","/ui"}:
            supplied=request.headers.get("authorization","")
            if not hmac.compare_digest(supplied,"Bearer "+token):
                return JSONResponse({"error":"unauthorized"},status_code=401)
        started=time.monotonic();parameters=dict(request.query_params)
        if path.startswith('/v1/') and 'application/json' in request.headers.get('content-type',''):
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>65536:return JSONResponse({'error':'json_request_limit_64KiB'},status_code=413)
            request._body=bytes(body)
            try:parameters=json.loads(body) if body else parameters
            except ValueError:pass
        from .telemetry import request_versions
        versions={};context_token=request_versions.set(versions)
        try:response=await call_next(request)
        finally:request_versions.reset(context_token)
        if path.startswith('/v1/') and path!='/v1/admin/history':
            admin.log('REST',request.method+' '+path,parameters,response.status_code,time.monotonic()-started,json.dumps(versions) if versions else None)
        return response

    @app.exception_handler(ValueError)
    async def invalid_request(request,error):
        return JSONResponse({"error":str(error)},status_code=400)

    @app.get("/healthz")
    def healthz():
        return {"status":"ok"}

    @app.get("/v1/status")
    def status(generation:str|None=None):
        return {**service.get_data_status(generation),"market":market.status()}

    @app.get("/v1/companies")
    def companies(query:Annotated[CompanyQuery,Query()]):
        return service.search_companies(**query.model_dump())

    @app.get("/v1/dictionary")
    def dictionary(query:Annotated[DictionaryQuery,Query()]):
        args=query.model_dump();source=args.pop("source")
        return market.dictionary(**args) if source=="market" else service.get_field_dictionary(**args)

    @app.get("/v1/financials")
    def financials(query:Annotated[FinancialQuery,Query()]):
        return service.get_financials(**query.model_dump())

    @app.post("/v1/financials")
    def financials_post(query:FinancialQuery):
        return service.get_financials(**query.model_dump())

    @app.get("/v1/metrics")
    def metrics(query:Annotated[MetricQuery,Query()]):
        return service.get_metrics(**query.model_dump())

    @app.post("/v1/metrics")
    def metrics_post(query:MetricQuery):
        return service.get_metrics(**query.model_dump())

    @app.post("/v1/screen")
    def screen(query:ScreenQuery):
        return service.screen_companies(**query.model_dump())

    @app.post("/v1/export")
    def export(query:ExportQuery):
        return save_export(query)

    @app.get("/v1/exports/{export_id}")
    def download(export_id:str):
        if len(export_id)!=32 or any(c not in "0123456789abcdef" for c in export_id):
            return JSONResponse({"error":"unknown_export"},status_code=404)
        folder=service.store.root/"exports"
        for extension in ("csv","parquet"):
            path=folder/f"{export_id}.{extension}"
            if path.is_file():
                return FileResponse(path,filename=f"financial-screen-{export_id}.{extension}")
        return JSONResponse({"error":"unknown_export"},status_code=404)

    app.mount("/",mcp.streamable_http_app())
    return app
