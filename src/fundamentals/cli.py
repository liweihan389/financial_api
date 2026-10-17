import argparse
import json
import os
from pathlib import Path
import sys
import zipfile

from .importer import import_file,verify_securities
from .sec_combined import FinancialService

def main():
    parser=argparse.ArgumentParser(description="Local Compustat research data service")
    parser.add_argument("--data-dir",default=None)
    commands=parser.add_subparsers(dest="command",required=True)
    load=commands.add_parser("import")
    load.add_argument("source")
    load.add_argument("--dictionary")
    load.add_argument("--memory",default="2GB")
    load.add_argument("--security-mappings")
    mappings=commands.add_parser("verify-securities")
    mappings.add_argument("csv")
    commands.add_parser("status")
    sec_import=commands.add_parser('import-sec');sec_import.add_argument('source')
    commands.add_parser('sec-status')
    commands.add_parser('reclean-sec')
    sec_sync=commands.add_parser('sync-sec');sec_sync.add_argument('--scope',choices=['sample','full','initialized'],default='sample')
    daily=commands.add_parser("import-market")
    daily.add_argument("source")
    daily.add_argument("--scope",choices=["sample","full"],default="sample")
    daily.add_argument("--memory",default="2GB")
    commands.add_parser("market-status")
    link=commands.add_parser("import-links")
    link.add_argument("source")
    application=commands.add_parser("package-app")
    application.add_argument("--output",default="market-fundamentals-app.zip")
    audit=commands.add_parser("audit")
    audit.add_argument("--as-of")
    audit.add_argument("--generation")
    serve=commands.add_parser("serve")
    serve.add_argument("--host",default="127.0.0.1")
    serve.add_argument("--port",type=int,default=8765)
    pack=commands.add_parser("package-plugin")
    pack.add_argument("--output",default="market-fundamentals-plugin.zip")
    args=parser.parse_args()
    try:
        if args.command=="import":
            result=import_file(args.source,args.data_dir,args.dictionary,args.memory,args.security_mappings,progress=lambda message:print(message,file=sys.stderr))
        elif args.command in {'import-sec','sec-status','sync-sec','reclean-sec'}:
            from .sec_store import SecStore
            from .store import Store
            sec=SecStore(Store(args.data_dir).root)
            if args.command=='import-sec':result=sec.import_file(args.source,progress=lambda x:print(json.dumps(x),file=sys.stderr))
            elif args.command=='reclean-sec':result=sec.import_documents([],reclean=True)
            else:
                from .admin import Admin
                from .sec_jobs import SecJobs
                admin=Admin(sec.vendor.root);jobs=SecJobs(admin,sec)
                if args.command=='sec-status':result={**sec.status(),'updates':jobs.status()}
                else:
                    queued=jobs.start_sync(args.scope);admin.pool.shutdown(wait=True);result={'queued':queued,'jobs':admin.jobs()}
                admin.pool.shutdown(wait=True)
        elif args.command=="verify-securities":
            result=verify_securities(args.csv,args.data_dir)
        elif args.command=="status":
            result=FinancialService(args.data_dir).get_data_status()
        elif args.command=="import-market":
            from .market_store import import_market
            result=import_market(args.source,args.data_dir,args.scope,progress=lambda x:print(json.dumps(x),file=sys.stderr),memory=args.memory)
        elif args.command=="import-links":
            from .market_links import import_links
            result=import_links(args.source,args.data_dir)
        elif args.command=="market-status":
            from .market_service import MarketService
            result=MarketService(args.data_dir).status()
        elif args.command=="package-app":
            package=Path(__file__).resolve().parents[2]
            allowed=[package/"src",package/"plugin",package/"pyproject.toml",package/"requirements.lock",package/"requirements-dev.txt",package/"Dockerfile",package/"compose.yaml",package/"README.md",package/"DEPLOYMENT.md",package/"SEC.md",package/".dockerignore",package/".agents",package/"market-links.template.csv",package/"security_mappings.template.csv",package/"tests",package/"验收记录.md",package/"scripts",package/".env.example"]
            with zipfile.ZipFile(args.output,"w",zipfile.ZIP_DEFLATED) as output:
                for base in allowed:
                    for path in sorted(base.rglob("*")) if base.is_dir() else [base]:
                        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc",".duckdb"} and "market_fundamentals.egg-info" not in path.parts:output.write(path,Path("market-fundamentals")/path.relative_to(package))
            result={"application":str(Path(args.output).resolve()),"raw_data_included":False}
        elif args.command=="audit":
            from .audit import audit_coverage
            result=audit_coverage(args.data_dir,args.generation,args.as_of,progress=lambda message:print(message,file=sys.stderr))
        elif args.command=="package-plugin":
            plugin=Path(__file__).resolve().parents[2]/"plugin"
            with zipfile.ZipFile(args.output,"w",zipfile.ZIP_DEFLATED) as output:
                for path in sorted(plugin.rglob("*")):
                    if path.is_file():
                        output.write(path,Path("market-fundamentals")/path.relative_to(plugin))
            result={"plugin":str(Path(args.output).resolve())}
        else:
            if args.host not in {"127.0.0.1","localhost","::1"} and not os.environ.get("FINANCIAL_API_TOKEN"):
                raise ValueError("remote_listener_requires_FINANCIAL_API_TOKEN")
            import uvicorn
            from .api import create_app
            local_host="[::1]" if args.host=="::1" else args.host if args.host not in {"0.0.0.0","::"} else "127.0.0.1"
            public_url=os.environ.get("FINANCIAL_PUBLIC_URL",f"http://{local_host}:{args.port}")
            uvicorn.run(create_app(args.data_dir,public_url=public_url),host=args.host,port=args.port)
            return
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError) as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1) from error

if __name__=="__main__":
    main()
