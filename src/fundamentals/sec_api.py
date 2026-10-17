import csv
import io
import json
import uuid
from pathlib import Path
from typing import Literal, Any
from fastapi import APIRouter,Query
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field
from mcp.types import ToolAnnotations

class Inspect(BaseModel):
    model_config=ConfigDict(extra='forbid')
    generation:str|None=None
    company:str|None=None
    field:str|None=None
    code:str|None=None
    limit:int=Field(default=100,ge=1,le=1000)
    offset:int=Field(default=0,ge=0)
class Sync(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scope:Literal['sample','full','initialized']='sample'
    companies:list[str]|None=Field(default=None,max_length=1000)
class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    user_agent:str=Field(max_length=512)


def register_sec(app,mcp,service,jobs,admin,public_url,trace):
    router=APIRouter(prefix='/v1/sec');read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    @mcp.tool(annotations=read)
    @trace
    def get_sec_status()->dict[str,Any]:
        """SEC independent generation, cutoff, mapping coverage and disabled/enabled daily update status."""
        return {**service.sec.status(),'updates':jobs.status()}
    @mcp.tool(annotations=read)
    @trace
    def get_sec_facts(request:Inspect)->dict[str,Any]:
        """Original standard-tag SEC facts with accession and unconverted value; use a CIK as company."""
        return service.sec.inspect('facts',**request.model_dump())
    @mcp.tool(annotations=read)
    @trace
    def get_sec_issues(request:Inspect)->dict[str,Any]:
        """Inspect uncertainty and identity/unit problems. Candidates are not approvals."""
        return service.sec.inspect('issues',**request.model_dump())
    @mcp.tool(annotations=read)
    @trace
    def get_sec_field_mappings(query:str='',limit:int=100,offset:int=0)->dict[str,Any]:
        """All original target fields have explicit SEC mapping status and versioned rules."""
        if not 1<=limit<=1000 or offset<0:raise ValueError('invalid_paging')
        rows=[r for r in service.sec.mapping() if query.lower() in (r['field']+' '+r['status']).lower()]
        return {'rule_version':service.sec.status()['configured_rule_version'],'total':len(rows),'rows':rows[offset:offset+limit]}
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
    @trace
    def sync_sec_data(request:Sync)->dict[str,Any]:
        """Queue SEC download into an independent immutable version. Requires configured SEC_USER_AGENT; returns a job ID."""
        return jobs.start_sync(**request.model_dump())
    router.add_api_route('/status',get_sec_status,methods=['GET'])
    @router.get('/facts')
    def facts(generation:str|None=None,company:str|None=None,field:str|None=None,limit:int=Query(100,ge=1,le=1000),offset:int=Query(0,ge=0)):
        return service.sec.inspect('facts',generation,company,field,None,limit,offset)
    @router.get('/issues')
    def issues(generation:str|None=None,company:str|None=None,field:str|None=None,code:str|None=None,limit:int=Query(100,ge=1,le=1000),offset:int=Query(0,ge=0)):
        return service.sec.inspect('issues',generation,company,field,code,limit,offset)
    router.add_api_route('/mappings',get_sec_field_mappings,methods=['GET'])
    router.add_api_route('/sync',sync_sec_data,methods=['POST'])
    @router.put('/settings')
    def configure(settings:Settings):return jobs.configure(settings.user_agent)
    @router.get('/issues/export')
    def export_issues(generation:str|None=None,company:str|None=None,field:str|None=None,code:str|None=None):
        pinned=generation or (service.sec.active() or {}).get('sec_generation');out=io.StringIO();writer=csv.DictWriter(out,fieldnames=['cik','gvkey','period','field','code','evidence']);writer.writeheader();offset=0
        while True:
            response=service.sec.inspect('issues',pinned,company,field,code,1000,offset)
            for row in response['rows']:writer.writerow({**row,'evidence':json.dumps(row['evidence'],ensure_ascii=False)})
            offset+=len(response['rows'])
            if offset>=response['total']:break
        return Response(out.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="sec-issues.csv"'})
    app.include_router(router)
    app.state.sec_jobs=jobs
